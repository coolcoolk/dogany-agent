"""Versioned ticket citations in the existing Why event log (R1/R2).

Purpose uses existing parent edges. Dependency and usage references never
participate in that graph. Execution spend belongs to an execution once;
Why views contain references to that same execution, never copied spend.
"""

import datetime
import json
import math
import os
import re

from . import events as E, nodes as N, store, surfaces
from . import WhyRefusal
from record_provider import configured

CHANGE_KINDS = ("node", "refine", "bend", "split", "end")
SEMANTIC = ("propose", "revise", "pivot", "retire", "attach")
DEFINE = ("pointed_line", "how", "what", "done")
# Unresolved checks require an exact, source-verified R3 judgment.


def versions(rows):
    return {e["why"]: e["id"] for e in rows
            if e.get("ev") in SEMANTIC}


def reachable(nodes, whys):
    seen, active = set(), set()

    def visit(w):
        if w in active:
            raise WhyRefusal("purpose-cycle")
        if w not in nodes:
            raise WhyRefusal("purpose-missing")
        if w in seen:
            return
        active.add(w)
        for p in nodes[w]["parents"]:
            visit(p)
        active.remove(w)
        seen.add(w)
    for w in whys:
        visit(w)
    return sorted(seen)


def work_rows(rows):
    return {e["data"]["ticket"]: e for e in rows if e.get("ev") == "work"}


def registered_ticket(path, records):
    # Registered paths outrank mutable frontmatter IDs. Editing an ID at the
    # same source cannot fall back to the legacy unregistered-ticket reader.
    absolute = os.path.abspath(path)
    return next((e for e in records.values() if e["data"]["path"] == absolute),
                None) or records.get(surfaces.ticket_id_of(path))


def _required(doc, keys):
    return isinstance(doc, dict) and all(
        isinstance(doc.get(k), str) and doc[k].strip() for k in keys)


def _read_ticket(path):
    # Preserve newline bytes as well as whitespace; decoding is strict.
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _alias_text(text, ticket):
    """Explicit alias registration may map only a single flat ticket ID.

    This is identifier matching, not lifecycle or meaning classification.
    All other bytes, including whitespace and lifecycle fields, are retained.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] != "---\n":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i] == "---\n"), None)
    if end is None:
        return None
    ids = [i for i in range(1, end) if re.match(r"^id\s*:", lines[i])]
    if len(ids) != 1:
        return None
    i = ids[0]
    if not re.fullmatch(r"id: (?:" + re.escape(ticket) + r"|\"" +
                        re.escape(ticket) + r"\"|'" + re.escape(ticket) + r"')\n", lines[i]):
        return None
    lines[i] = "id: <registered-alias>\n"
    return "".join(lines)


def change_judgment(work, before_sha256, after_sha256, root=None):
    """Return an exact R3 judgment verified against the real owner source."""
    from .r3 import ticket_judgment
    return ticket_judgment(work, before_sha256, after_sha256, root)


def _text_changed(text, work, alias_id=None, root=None, path=None, diagnostics=None):
    if work.get("intake_transition"):
        provider = configured()
        verify_receipt = provider.verify_receipt if provider else lambda *args: False
        from .intake import digest
        receipt = work.get("intake_receipt", {})
        if (digest(receipt) != work["intake_transition"]
                or receipt.get("after_sha256") != work["ticket_sha256"]
                or not verify_receipt(root or store.root_dir(), receipt)):
            return True
    after_sha = store.sha256_text(text)
    if after_sha == work["ticket_sha256"]:
        return False
    if alias_id:
        before = _alias_text(work.get("ticket_text", ""), work["ticket"])
        after = _alias_text(text, alias_id)
        return before is None or after is None or before != after
    provider = configured()
    if provider and provider.proves_change(root or store.root_dir(), store.bot_data_dir(root),
            work, path, text, after_sha, diagnostics):
        return False
    return change_judgment(work, work["ticket_sha256"], after_sha, root) is None


def ticket_observation(path, work, root=None):
    """A failed source read is an explicit supplement, isolated to its work."""
    try:
        text = _read_ticket(path)
    except (OSError, UnicodeError) as exc:
        return {"changed": True, "file_changed": True,
                "kind": "ticket-source-missing", "reason": type(exc).__name__,
                "path": os.path.abspath(path)}
    diagnostics = {}
    changed = _text_changed(text, work, root=root, path=path, diagnostics=diagnostics)
    return {"changed": changed, **diagnostics,
            "kind": "unjudged-change",
            "before_sha256": work["ticket_sha256"],
            "after_sha256": store.sha256_text(text),
            "file_changed": store.sha256_text(text) != work["ticket_sha256"]}


def semantic_works(work, rows):
    """All registrations of the same ticket, target or linked semantic work.

    Hashes are observations, never authority to detach a reworded task.
    canonical_work is provenance; older and newer aliases share open checks.
    Until a verified new-target/effect judgment exists, a reused ticket or
    target conservatively remains connected to its prior semantic work.
    """
    works = [e for e in rows if e.get("ev") == "work"]
    ids, tickets, targets, keys = set(), {work["ticket"]}, {work["target"]}, set()
    if work.get("change_key"):
        keys.add(work["change_key"])
    if work.get("canonical_work"):
        ids.add(work["canonical_work"])
    selected = set()
    while True:
        added = False
        for e in works:
            d = e["data"]
            if e["id"] in selected:
                continue
            if e["id"] in ids or d.get("canonical_work") in ids \
                    or d["ticket"] in tickets or d["target"] in targets \
                    or (d.get("change_key") and d["change_key"] in keys):
                selected.add(e["id"])
                ids.add(e["id"])
                if d.get("canonical_work"):
                    ids.add(d["canonical_work"])
                tickets.add(d["ticket"])
                targets.add(d["target"])
                if d.get("change_key"):
                    keys.add(d["change_key"])
                added = True
        if not added:
            return [e for e in works if e["id"] in selected]


def ticket_changed(path, work, root=None):
    return ticket_observation(path, work, root)["changed"]


def change_key(target, definition):
    """Observed identity fields, not a claim of semantic equivalence.

    Missing/unknown before or after effects require supplementation. Ticket
    names, delegation, wording of How, era and authority are not change IDs.
    """
    fields = [target] + [definition.get(k) for k in ("effect_before", "effect_after", "done")]
    if any(not isinstance(v, str) or not v.strip() or v.strip().lower() in
           ("unknown", "none", "tbd", "?") for v in fields):
        return None
    return store.sha256_text(json.dumps([" ".join(v.split()) for v in fields]))


def _unique_checks(checks):
    return list({json.dumps(c, sort_keys=True): c for c in checks}.values())


def record_ticket(path, definition, target, era, supersedes, confirmed_by,
                  whys=(), depends_on=(), uses=(), root=None):
    """Snapshot the existing product ticket and its complete citation path.

    confirmed_by is a source reference, not a generated owner approval.
    No owner question is created by this verb. A linked semantic work needs
    definition.effect_before/effect_after; unknown identity stays supplement.
    """
    fm = surfaces.frontmatter(path)
    if not fm or not os.path.isfile(path):
        raise WhyRefusal("ticket-missing")
    tid = surfaces.ticket_id_of(path, fm)
    if not _required(definition, DEFINE) or not all(
            isinstance(v, str) and v.strip()
            for v in (target, era, supersedes, confirmed_by)):
        raise WhyRefusal("work-incomplete")
    provider = configured()
    receipt_anchor = provider.ticket_anchor(root or store.root_dir(), store.bot_data_dir(root), tid) if provider else None
    text = _read_ticket(path)
    sha = store.sha256_text(text)

    def build(rows):
        nodes = N.fold(rows)
        direct = list(dict.fromkeys(whys)) or surfaces.ticket_why(
            path, nodes, records=work_rows(rows), root=root)["whys"]
        if not direct:
            raise WhyRefusal("purpose-missing")
        reach = reachable(nodes, direct)
        vs = versions(rows)
        key = change_key(target, definition)
        previous = work_rows(rows).get(tid)
        group = semantic_works({"ticket": tid, "target": target, "change_key": key}, rows)
        canonical = group[0] if group else None
        pending = []
        if group:
            # record-ticket is the path update operation too: only an exact
            # raw snapshot, same ticket and change key can replace a source.
            old = previous["data"] if previous else {}
            moved = old.get("path") != os.path.abspath(path) and \
                old.get("ticket_sha256") == sha and old.get("change_key") == key
            overrides = {tid: os.path.abspath(path)} if moved else None
            pending += rechecks(group[-1]["data"], rows, overrides, root)[0]
            # Compare a new alias with the latest semantic snapshot, rather
            # than only its first canonical registration. Map only the alias ID;
            # all other edits remain subject to the fail-closed comparison.
            snapshot = previous or group[-1]
            if _text_changed(text, snapshot["data"],
                             tid if snapshot["data"]["ticket"] != tid else None, root, path):
                pending.append({"kind": "unjudged-change", "state": "supplement",
                                "work": snapshot["id"], "ticket": tid,
                                "before_sha256": snapshot["data"]["ticket_sha256"],
                                "after_sha256": sha})
            if key != group[-1]["data"].get("change_key"):
                pending.append({"kind": "change-identity-unclear", "state": "supplement",
                                "work": group[-1]["id"], "proposed_key": key})
        citations = {w: {"version": vs[w], "text": nodes[w]["text"],
                         "target": nodes[w].get("target") or w,
                         "scope": nodes[w]["scope"],
                         "parents": list(nodes[w]["parents"])} for w in reach}
        proposed = list(direct)
        if canonical:
            original = canonical["data"]
            # Ticket aliases start at the original semantic work, never at
            # a caller's newly shortened purpose path or refreshed versions.
            citations = original["citations"]
            direct = original["relations"]["purpose"]
            if set(proposed) != set(direct):
                pending.append({"kind": "purpose-change", "state": "supplement",
                                "from": direct, "to": proposed,
                                "work": canonical["id"]})
        if not key:
            pending.append({"kind": "change-identity-unclear", "state": "supplement"})
        from . import r3  # r3 imports this module; bind at call time
        located = r3.registration_identity(definition)
        return E._event(rows, "work", None, "agent", dict({
            "ticket": tid, "project": fm.get("project"),
            "path": os.path.abspath(path), "ticket_sha256": sha,
            "ticket_text": text, "ticket_receipt_anchor": receipt_anchor,
            "change_key": key, "canonical_work": canonical["id"] if canonical else None,
            "target": target, "era": era, "supersedes": supersedes,
            "confirmed_by": confirmed_by, "define": dict(definition),
            "relations": {"purpose": direct, "depends-on": list(depends_on),
                          "uses": list(uses)},
            "proposed_purpose": proposed, "pending_rechecks": _unique_checks(pending),
            "citations": citations}, **({"r3_identity": located} if located else {})))
    with store.locked(root):
        rows = store.read_rows(store.EVENTS, root)
        candidate = build(rows)
        previous = work_rows(rows).get(tid)
        if previous:
            # canonical_work is first assigned on the initial append; it is
            # provenance, not a change that defeats an idempotent retry.
            old, new = dict(previous["data"]), dict(candidate["data"])
            old.pop("canonical_work", None)
            new.pop("canonical_work", None)
            if old == new:
                return previous
            if supersedes != previous["id"]:
                raise WhyRefusal("work-supersedes-required")
        return store.append_unlocked(store.EVENTS, lambda rs: candidate, root)


def dispatch_receipt(run, ticket, root=None):
    """Read dispatcher-owned run.env + post-spawn pid; never execute shell.

    An allocated run directory alone is not a start receipt. A finalized
    status, if present, must agree with the same run and ticket.
    """
    from host_dispatcher import _run_env
    if not re.fullmatch(r"dsp-\d{8}-\d{6}-\d+(?:-\d+)?", run or ""):
        raise WhyRefusal("run-unknown")
    base = os.path.join(store.bot_data_dir(root), "dispatch")
    rd = os.path.join(base, run)
    if os.path.realpath(rd) != os.path.join(os.path.realpath(base), run):
        raise WhyRefusal("run-unknown")
    env = _run_env(os.path.join(rd, "run.env"))
    try:
        with open(os.path.join(rd, "pid")) as f:
            pid = int(f.read().strip())
        epoch = int(env.get("SPAWN_EPOCH", "0"))
        if pid <= 0 or epoch <= 0 or env.get("RUNID") != run \
                or env.get("TICKET") != ticket:
            raise ValueError()
    except (OSError, ValueError):
        raise WhyRefusal("start-receipt-missing")
    status_path = os.path.join(rd, "status.json")
    status = {}
    if os.path.exists(status_path):
        try:
            with open(status_path) as f:
                status = json.load(f)
            # The dispatcher writes %z (+0900); Python 3.9 fromisoformat
            # accepts the colon form. Keep the shared Why parser unchanged.
            started = status.get("started_at", "")
            started = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", started)
            if status.get("runid") != run or status.get("ticket") != ticket \
                    or not store.parse_dt(started):
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            raise WhyRefusal("start-receipt-mismatch")
    return {"kind": "run", "id": run, "start": {"epoch": epoch, "pid": pid},
            "source": os.path.join(rd, "run.env"),
            "result": status.get("result"), "status": status_path if status else None}


def session_receipt(session, span, root=None):
    """A session execution names two observed transcript entries, in order."""
    import owner_turns
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session or ""):
        raise WhyRefusal("session-unknown")
    parts = (span or "").split(":")
    if len(parts) != 2 or not all(parts):
        raise WhyRefusal("session-span-missing")
    path = os.path.join(owner_turns.project_dir(root or store.root_dir()), session + ".jsonl")
    try:
        rows = list(store.jsonl_iter(path))
        indices = [next(i for i, e in enumerate(rows) if e.get("uuid") == p
                        and e.get("timestamp")) for p in parts]
        if indices[0] > indices[1]:
            raise ValueError()
    except (OSError, ValueError, StopIteration):
        raise WhyRefusal("session-span-missing")
    return {"kind": "session", "id": session + ":" + span,
            "session": session, "span": span, "source": path,
            "start": rows[indices[0]]["timestamp"], "end": rows[indices[1]]["timestamp"]}


def execution(ticket, run=None, session=None, span=None, cost=None, root=None):
    if bool(run) == bool(session):
        raise WhyRefusal("execution-ref-required")
    receipt = dispatch_receipt(run, ticket, root) if run else session_receipt(session, span, root)
    cost = {} if cost is None else cost
    if not isinstance(cost, dict) or any(
            not isinstance(k, str) or not k or isinstance(v, bool)
            or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0
            for k, v in cost.items()):
        raise WhyRefusal("cost-invalid")
    key = receipt["kind"] + ":" + receipt["id"]
    with store.locked(root):
        rows = store.read_rows(store.EVENTS, root)
        if ticket not in work_rows(rows):
            raise WhyRefusal("work-missing")
        for e in rows:
            if e.get("ev") == "execution" and e["data"]["key"] == key:
                if e["data"]["ticket"] != ticket or e["data"]["cost"] != cost:
                    raise WhyRefusal("execution-conflict")
                return e
        data = {"ticket": ticket, "key": key, "receipt": receipt, "cost": cost,
                "work_version": work_rows(rows)[ticket]["id"]}
        return store.append_unlocked(store.EVENTS, lambda rs: E._event(
            rs, "execution", None, "agent", data), root)


def rechecks(work, rows, source_overrides=None, root=None):
    # No caller-supplied supersedes/alias is a verified resolution record.
    checks = []
    group = semantic_works(work, rows)
    for data in [e["data"] for e in group] or [work]:
        checks += _work_rechecks(data, rows, root)[0]
    # An already registered alias cannot hide an unregistered semantic edit
    # on another current ticket. Historical snapshots are retained above;
    # only each ticket's latest snapshot is compared with its current file.
    for e in work_rows(group).values():
        data = e["data"]
        path = (source_overrides or {}).get(data["ticket"], data["path"])
        observed = ticket_observation(path, data, root)
        if observed["changed"]:
            check = {"kind": observed.get("kind", "unjudged-change"),
                     "state": "supplement", "work": e["id"], "ticket": data["ticket"]}
            if "before_sha256" in observed:
                check.update(before_sha256=observed["before_sha256"],
                             after_sha256=observed["after_sha256"])
            if "reason" in observed:
                check.update(reason=observed["reason"], path=observed["path"])
            check.update({k: v for k, v in observed.items() if k.startswith("receipt_")})
            if check["kind"] == "unjudged-change":
                check["supported_path"] = (
                    "For status/progress-only edits use devkit_ticket set <ticket> "
                    "--status <state> or devkit_ticket set <ticket> --progress <event> "
                    "[--ref <reference>]. An existing hand edit still needs judgment; "
                    "a later writer call cannot authorize it.")
            checks.append(check)
    # Only the ticket-byte warning can be resolved by this seam. Purpose,
    # effects, refinement and authority checks retain their own obligations.
    unresolved = []
    for check in checks:
        original = next((e for e in rows if e.get("ev") == "work" and e["id"] == check.get("work")), None)
        judgment = None
        if check.get("kind") == "unjudged-change" and original:
            data = original["data"]
            try:
                after = store.sha256_text(_read_ticket(data["path"]))
                if check.get("ticket") == data["ticket"] \
                        and check.get("before_sha256") == data["ticket_sha256"] \
                        and check.get("after_sha256") == after:
                    judgment = change_judgment(data, data["ticket_sha256"], after, root)
            except (OSError, UnicodeError):
                pass
        if judgment is None:
            unresolved.append(check)
    return _unique_checks(unresolved), []


def _work_rechecks(work, rows, root=None):
    out = list(work.get("pending_rechecks", []))
    if work.get("intake_transition"):
        provider = configured()
        verify_receipt = provider.verify_receipt if provider else lambda *args: False
        if not verify_receipt(root or store.root_dir(), work.get("intake_receipt", {})):
            out.append(dict(kind="intake-transition-unavailable", state="supplement"))
    nodes, vs = N.fold(rows), versions(rows)
    reach = reachable(nodes, work["relations"]["purpose"])
    for wid in sorted(set(reach) | set(work["citations"])):
        node = nodes[wid]
        original = work["citations"].get(wid)
        if original is None:
            out.append({"why": wid, "to": vs[wid], "kind": "uncited-parent",
                        "state": "supplement"})
        elif set(original["parents"]) != set(node["parents"]):
            out.append({"why": wid, "from": original["version"], "to": vs[wid],
                        "kind": "parent-change", "state": "supplement"})
        if node["status"] in N.TERMINAL:
            out.append({"why": wid, "to": vs[wid], "kind": "terminal-purpose",
                        "state": "supplement"})
        if original is None:
            continue
        found = False
        for ev in rows:
            if ev.get("id") == original["version"]:
                found = True
                continue
            if not found or ev.get("why") != wid or ev.get("ev") not in SEMANTIC:
                continue
            d = ev.get("data") or {}
            kind = d.get("change_kind") or {"pivot": "bend", "retire": "end",
                                           "attach": "parent-change"}.get(ev["ev"], "bend")
            out.append({"why": wid, "from": original["version"], "to": ev["id"],
                        "kind": kind, "state": "supplement" if kind in
                        ("refine", "parent-change") else "re-check"})
    return _unique_checks(out), []


def owner_confirmations(rows, root=None, inherited=()):
    """Actual confirmation is independent of the legacy action-derived status."""
    same_store = os.path.realpath(store.path(store.EVENTS, root)) == os.path.realpath(store.path(store.EVENTS))
    bad = {b["evt"] for b in E.audit_confirms()} if same_store else {
        e["id"] for e in rows if e.get("ev") == "confirm"}
    out = {}
    preserved = {x["to"] for x in inherited}
    for e in rows:
        wid = e.get("why")
        if e.get("ev") == "confirm" and e["id"] not in bad:
            out[wid] = {"state": "confirmed", "record": e}
        elif e.get("ev") in ("revise", "pivot", "retire") and wid in out \
                and e["id"] not in preserved:
            out[wid]["state"] = "superseded"
    return out


def trace(path, root=None):
    """Existing show/lint surface: complete path, receipts, and all re-checks."""
    rows = store.read_rows(store.EVENTS, root)
    nodes = N.fold(rows)
    registered = work_rows(rows)
    event = registered_ticket(path, registered)
    tid = event["data"]["ticket"] if event else surfaces.ticket_id_of(path)
    if event is None:
        return {"ticket": tid, "state": "supplement", "reason": "work-missing",
                "why": surfaces.ticket_why(path, nodes)}
    work = event["data"]
    checks, inherited = rechecks(work, rows, root=root)
    confirmations = owner_confirmations(rows, root, inherited)
    path_nodes = {}
    reach = reachable(nodes, work["relations"]["purpose"])
    for wid in sorted(set(reach) | set(work["citations"])):
        history = [e for e in rows if e.get("why") == wid]
        confirmation = confirmations.get(wid, {"state": "absent", "record": None})
        current = versions(rows).get(wid)
        path_nodes[wid] = {"node": nodes[wid], "version": current, "history": history,
                           "owner_confirmation": confirmation["state"],
                           "confirmation_record": confirmation["record"]}
    group = semantic_works(work, rows)
    work_ids = {e["id"] for e in group}
    executions = {}
    for ev in rows:
        if ev.get("ev") != "execution" or ev["data"]["work_version"] not in work_ids:
            continue
        d = dict(ev["data"])
        r = d["receipt"]
        try:
            observed = dispatch_receipt(r["id"], d["ticket"], root) if r["kind"] == "run" else session_receipt(r["session"], r["span"], root)
            if observed["start"] != r["start"]:
                raise WhyRefusal("start-receipt-mismatch")
            d["state"] = "dispatched" if r["kind"] == "run" else "observed-session"
            d["observed"] = observed
        except WhyRefusal as exc:
            d.update(state="supplement", reason=exc.reason)
        executions[d["key"]] = d
    all_checks = {t: rechecks(e["data"], rows, root=root)[0] for t, e in registered.items()}
    observed = ticket_observation(path, work, root)
    changed, file_changed = observed["changed"], observed["file_changed"]
    invalid = any(d["state"] == "supplement" for d in executions.values())
    return {"ticket": tid, "state": "supplement" if checks or changed or invalid else "linked",
            "ticket_changed": changed, "ticket_file_changed": file_changed,
            "aliases": sorted({e["data"]["ticket"] for e in group}),
            "work_version": event["id"], "work": work,
            "path": path_nodes, "rechecks": checks, "refine_inheritance": inherited,
            "all_rechecks": all_checks, "executions": executions,
            "execution_state": "dispatched" if any(d["state"] == "dispatched" for d in executions.values()) else "received",
            "why_executions": {w: [k for k, d in executions.items() if w in next(
                e["data"]["citations"] for e in rows if e["id"] == d["work_version"])] for w in path_nodes},
            "cost": {k: sum(d["cost"].get(k, 0) for d in executions.values())
                     for k in {k for d in executions.values() for k in d["cost"]}}}
