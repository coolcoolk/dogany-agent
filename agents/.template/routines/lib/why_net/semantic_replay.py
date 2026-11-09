"""Offline FDGN-594 semantic replay over source-cited executor inputs.

Effect/scope reasoning is agent supplied, as in the v4 contract. It is kept
separate from frozen expectations: neither frame construction nor the writer
reads expected labels. This exercises core policy, not installed carriers.
"""
import hashlib
import json
from pathlib import Path

from . import intake, nodes, store, tree
from .evidence import EvidenceResolver
from .frame import judge_work
from .records import TicketlessRecords
from why_frame_gate import validate_build_start
from term_registry import schema_labels


OWNER = "fdgn594-owner"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def order(at):
    return int(store.parse_iso(at) * 1_000_000)


def validate_sources(corpus, manifest):
    """Check extracted owner bytes against the independently frozen hashes."""
    sources = corpus["sources"]
    for row in manifest["rows"]:
        source = row.get("input")
        if source is None:
            assert row["id"] not in sources, row["id"]
            continue
        actual = sources[row["id"]]
        assert all(actual[k] == source[k] for k in ("file", "uuid", "line")), row["id"]
        assert sha(actual["text"]) == source["owner_words_sha256"], row["id"]
        assert corpus["ledger"][row["id"]]["sha"] == source["ledger_sha"], row["id"]
        if row.get("proposal"):
            proposal = sources["proposal:" + row["id"]]
            assert all(proposal[k] == row["proposal"][k] for k in ("file", "uuid", "line"))
    for proof in manifest["extra_proofs"]:
        source = sources[proof["alias"]]
        assert sha(source["text"]) == proof["owner_text_sha256"]
        for previous in proof["previous"]:
            source = sources["proposal:" + proof["alias"]]
            assert sha(source["text"]) == previous["text_sha256"]
    raw_events = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"
                         for e in corpus["events"])
    assert sha(raw_events) == manifest["source_hashes"]["local_proofs"][
        ".telegram_bot/state/why-net/events.jsonl"]
    for source in sources.values():
        assert sha(source["text"]) == source["text_sha256"]


def validate_snapshot(corpus, snapshot):
    """Re-extract every retained region from complete real native chains.

    Unlike the portable corpus checks this verifies the main-chain/ledger
    join, including both negative joins, without trusting an on_chain flag.
    """
    import why_owner_turn as native
    ledger = [json.loads(s) for s in (snapshot / "turn-ledger.jsonl").read_text().splitlines()]
    sessions = {}
    for source in corpus["sources"].values():
        name = source["file"]
        if name not in sessions:
            turns, _, chain, entries = native.chain_turns(str(snapshot / name))
            bound, _ = native.bind_ledger([r for r in ledger if r.get("session_id") == Path(name).stem], turns)
            sessions[name] = (chain, {e.get("uuid"): e for e in entries}, bound)
        chain, entries, _ = sessions[name]
        entry = entries[source["uuid"]]
        assert source["uuid"] in chain
        text = (native.owner_words(native.entry_text(entry)) if source["kind"] == "owner"
                else "\n".join(native.assistant_texts(entry)))
        if source.get("omitted_spans"):
            assert source["kind"] == "assistant" and sha(text) == source["raw_text_sha256"]
            for start, end in reversed(source["omitted_spans"]):
                text = text[:start] + "[local image path omitted]" + text[end:]
        assert text == source["text"]
        at_line = json.loads((snapshot / name).read_text().splitlines()[source["line"] - 1])
        assert at_line["uuid"] == source["uuid"]
    bound_ids = {r["id"] for _, _, bound in sessions.values() for r in bound.values()}
    for key, row in corpus["ledger"].items():
        actual = next(r for r in ledger if r.get("id") == key)
        assert all(actual[k] == value for k, value in row.items())
        # The two unavailable owner turns must not borrow adjacent evidence.
        source = next((s for s in corpus["sources"].values() if s.get("ledger_id") == key), None)
        if source:
            assert sessions[source["file"]][2][source["uuid"]]["id"] == key
        elif key.startswith("otl-"):
            assert key not in bound_ids


def build_case(spec, corpus, root):
    """Construct real writer state and resolver facts from reviewed inputs."""
    sources = corpus["sources"]
    key = spec["id"]
    ledger = corpus["ledger"][key]
    source = sources.get(key)
    # Ledger timestamps are second-granular and can precede native ingestion.
    # Use the exact selected native endpoint, never a later owner answer.
    as_of = order(source["at"] if source else ledger["at"])
    binding = dict(owner_id=OWNER, runtime="claude-code", session_id=ledger["session_id"],
                   request_id=ledger["request_id"], owner_text_hash=intake.digest(source["text"] if source else ""))
    unit = dict(work_key=spec["outcome"], scope_revision=1,
                scope_digest=intake.digest(spec["outcome"]), targets=["replay/" + key],
                effect_ref="effect:" + key)
    context = dict(binding, **unit)
    facts = {}
    for ref, region in sources.items():
        if region["kind"] == "owner":
            facts[ref] = dict(kind="owner-message", owner_id=OWNER, owner_text=region["text"],
                source_role="owner", source_order=order(region["at"]), source_hash=intake.digest(region["text"]))
    for citation in spec["citations"]:
        assert citation in sources and order(sources[citation]["at"]) <= as_of, (key, citation)
    effect = dict(unit, kind="work-effect", owner_id=OWNER, effect=spec["effect"],
                  future_behavior=spec["future_behavior"], outcome=spec["outcome"],
                  provenance=spec.get("provenance", "bound"))
    if spec["effect"] in ("confirmed-continuation", "bugfix"):
        assert spec["citations"]
        # Same-work proposal/actual owner answer or reconstructed prior scope;
        # unrelated adjacent answers never enter this authorization bundle.
        effect["accepted_scope_ref"] = "accepted:" + key
        facts[effect["accepted_scope_ref"]] = dict(kind="accepted-scope", owner_id=OWNER,
            outcome=spec["outcome"], targets=unit["targets"], owner_authority_ref="authority:" + key)
        facts["authority:" + key] = dict(kind="owner-authorization", owner_id=OWNER,
                                       citations=spec["citations"])
    facts[unit["effect_ref"]] = effect
    # Fold only events available at this historical endpoint.
    historical = [e for e in corpus["events"] if order(e["at"]) <= as_of]
    known = nodes.fold(historical)
    for event in historical:
        if event["ev"] != "confirm":
            continue
        node = known[event["why"]]
        node.update(owner_id=OWNER, revision=event["id"], confirmation_ref=event["id"])
        facts[event["id"]] = dict(kind="node-confirmation", owner_id=OWNER,
            node_id=node["id"], revision=node["revision"], wording_hash=intake.digest(node["text"]),
            scope=node["scope"], source_order=order(event["at"]), valid_through=as_of)
    proofs = {}
    symptom = source["text"].splitlines()[0] if source else ""
    if symptom:
        proofs["symptom"] = dict(source_ref=key)
    why = ""
    if spec.get("why_source"):
        why = sources[spec["why_source"]]["text"]
        proofs["why"] = dict(kind="owner-reason", source_ref=spec["why_source"], quote=why)
    if spec.get("why_node"):
        node = known[spec["why_node"]]
        why = node["text"]
        proofs["why"] = dict(kind="confirmed-node", node_id=node["id"], revision=node["revision"],
                             relation=spec["relation"])
        unit.update(purpose_scope=node["scope"], candidate_fits=[dict(proofs["why"])])
    slots = dict(symptom='\u201c' + symptom + '\u201d' if symptom else "",
                 problem=spec.get("problem", spec["outcome"]), why=why,
                 direction=spec.get("direction", spec["outcome"]), direction_approval="", proofs=proofs,
                 approval_kind=spec.get("approval_kind", "pending"))
    if spec.get("approval_source"):
        approval_source = spec["approval_source"]
        words = sources[approval_source]["text"]
        start, end = spec.get("approval_span", [0, len(words)])
        slots["direction_approval"] = '\u201c' + words[start:end] + '\u201d'
        approval_ref = "approval:" + key
        proofs["direction_approval"] = dict(source_ref=approval_source, approval_ref=approval_ref)
        semantic = intake.digest(intake.semantic_binding(context, slots))
        facts[approval_ref] = dict(kind="direction-approval", owner_id=OWNER, state="approved",
            source_ref=approval_source, approval_kind=spec["approval_kind"], semantic_digest=semantic)
        if spec["approval_kind"] == "normal":
            readback = sources[spec["readback_source"]]
            assert readback["kind"] == "assistant" and order(readback["at"]) < as_of
            ref = "readback:" + key
            facts[ref] = dict(kind="delivered-readback", owner_id=OWNER, semantic_digest=semantic,
                delivery_state="delivered", carrier_id=readback["uuid"], turn_ended=True,
                delivered_order=order(readback["at"]), source_ref=spec["readback_source"])
            facts[approval_ref]["readback_ref"] = ref
        else:
            facts[approval_ref]["scoped_request_hash"] = facts[approval_source]["source_hash"]
    labels = schema_labels(str(Path(__file__).resolve().parents[3]), "why_intake_v4")
    adapter = TicketlessRecords(root, labels)
    receipt = intake.record_owner_slots(adapter, unit["work_key"], 0, slots, "intake-pending")
    saved = adapter.read_slots(unit["work_key"])
    unit["record_ref"] = saved["record_uri"]
    facts[saved["record_uri"]] = dict(kind="work-record", owner_id=OWNER, work_key=unit["work_key"],
        slots=saved, semantic_digest=intake.digest(intake.semantic_binding(context, saved)))
    frame = dict(binding=binding, units=[unit], nodes=known, as_of=as_of)
    if spec.get("restoration"):
        repair = dict(unit, work_key=spec["restoration"], effect_ref="repair:" + key)
        facts[repair["effect_ref"]] = dict(effect, **{k: repair[k] for k in (
            "work_key", "effect_ref")}, effect="bugfix", outcome=spec["restoration"],
            accepted_scope_ref="repair-scope:" + key)
        facts["repair-scope:" + key] = dict(kind="accepted-scope", owner_id=OWNER,
            targets=repair["targets"], outcome=spec["restoration"], owner_authority_ref="repair-authority:" + key)
        facts["repair-authority:" + key] = dict(kind="owner-authorization", owner_id=OWNER,
                                              citations=spec["citations"])
        frame["units"].insert(0, repair)
    current = dict(binding, work_keys=[u["work_key"] for u in frame["units"]])
    resolver = EvidenceResolver(facts.__getitem__, lambda: current)

    def node_reason(proof, text, ctx):
        node = known.get(proof.get("node_id"), {})
        return bool(node and proof.get("relation") and proof.get("revision") == node.get("revision")
            and text == node["text"] and tree.owner_backed_reusable(
                node, ctx["owner_id"], as_of, unit.get("purpose_scope"), resolver))

    # A conversational frame authorizes only reads/retransmission. It cannot
    # enroll a fresh mutation or design/build dispatch in its operation set.
    bound_tools = () if spec["effect"] == "question" else ("Write", "dispatch")
    return dict(frame=frame, resolver=resolver, node_reason=node_reason, current=current,
                bound_tools=bound_tools,
                facts=facts, slots=saved, receipt=receipt, adapter=adapter)


def execute(case):
    frame, resolver, node_reason = case["frame"], case["resolver"], case["node_reason"]
    verdicts = [judge_work(dict(frame, unit=u), resolver, node_reason) for u in frame["units"]]
    main = verdicts[-1]
    read = validate_build_start(frame, {"tool_name": "Read"}, resolver, node_reason)
    gates = {}
    for tool in ("Write", "dispatch"):
        operation = dict(tool_name=tool, targets=[u["targets"] for u in frame["units"]])
        case["current"].pop("operation_hash", None)
        if tool in case["bound_tools"]:
            case["current"]["operation_hash"] = intake.digest(operation)
        gates[tool] = validate_build_start(frame, operation, resolver, node_reason)["decision"]
    # A different operation never inherits the replay's bound action.
    unbound = validate_build_start(frame, dict(tool_name="Write", path="unrelated"), resolver, node_reason)
    reason = main.get("reason")
    if reason in ("hold-authority", "hold-provenance"):
        verdict = "hold"
    elif main["intake"] == "technical-refusal":
        verdict = "error"
    elif main["execution"] == "read/intake-only":
        verdict = "ask"
    elif main["tree"]["state"] == "linked":
        verdict = "silent-link"
    else:
        verdict = "skip"
    return dict(verdict=verdict, intake=main["intake"], reason=reason,
        execution=main["execution"], tree=main["tree"], gates=gates, read=read["decision"],
        unbound=unbound["decision"], units=verdicts, prompt_count=int(verdict == "ask"))


EXPECTED = {
    "new-authorized": ("silent-link", "confirmed"),
    "need-direction-approval": ("ask", "need-direction-approval"),
    "need-why-and-direction-combined": ("ask", "need-why-and-direction-combined"),
    "split:restoration-and-scoped-go-ahead": ("silent-link", "go-ahead"),
    "hold-authority": ("hold", "technical-refusal"),
    "hold-provenance": ("hold", "technical-refusal"),
    "accepted-continuation": ("skip", "exempt(confirmed-continuation)"),
    "bounded-errand/exempt": ("skip", "exempt(errand)"),
    "restoration/exempt": ("skip", "exempt(bugfix)"),
    "read-only/no-new-work": ("skip", "exempt(question)"),
    "read-only/restoration-diagnosis": ("skip", "exempt(question)"),
}


def run(inputs, corpus, manifest, scratch):
    validate_sources(corpus, manifest)
    expected = {r["id"]: r for r in manifest["rows"]}
    assert [r["id"] for r in inputs["rows"]] == list(expected)
    results = []
    for spec in inputs["rows"]:
        root = scratch / spec["id"]
        case = build_case(spec, corpus, root)
        actual = execute(case)
        frozen = expected[spec["id"]]
        want = EXPECTED[frozen["new"]]
        allowed = "deny" if (want[0] in ("ask", "hold")
                              or frozen["new"].startswith("read-only/")) else "allow"
        match = ((actual["verdict"], actual["intake"]) == want
            and all(v == allowed for v in actual["gates"].values())
            and actual["read"] == "allow" and actual["unbound"] == "deny"
            and actual["prompt_count"] == frozen["prompts"])
        if want[0] == "hold":
            match &= actual["reason"] == frozen["new"]
        if len(case["frame"]["units"]) > 1:
            match &= actual["units"][0]["intake"] == "exempt(bugfix)"
        results.append(dict(id=spec["id"], expected=frozen["new"], expected_verdict=want[0],
            match=bool(match), actual=actual, writer_receipt=case["receipt"],
            slots={f: dict(value_hash=intake.digest(case["slots"][f]),
                          proof=case["slots"]["proofs"].get(f)) for f in intake.FIELDS}))
    return dict(kind="semantic-core-replay", selected=len(results),
        matches=sum(r["match"] for r in results), prompts=sum(r["actual"]["prompt_count"] for r in results),
        authority_holds=sum(r["actual"]["reason"] == "hold-authority" for r in results),
        provenance_holds=sum(r["actual"]["reason"] == "hold-provenance" for r in results),
        scope="Source-cited agent semantic inputs through real judge, build gate and ticketless writer; no installed carrier claim.",
        rows=results)
