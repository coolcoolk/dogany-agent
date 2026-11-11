"""M2 observation/acceptance views of the existing decision ledger.

No capture engine or second live store: producers add a decision-validation
JSON comment to their existing entry with the normal ledger writer/editor.
Snapshots are audit artifacts. Only an allowlisted pre-outcome input goes to
the reviewer; historical/known outcomes are diagnostic, never blind scores.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3

MARK = re.compile(r"<!-- decision-validation (.*?) -->")
STATES = {"allowed", "answered", "unanswered", "held", "rejected", "discarded",
          "not-executed", "cancelled", "failed-run", "uncollected"}
KINDS = {"execution", "question"}
INPUT_KEYS = {"target", "version", "context", "authority", "purpose_path"}
PRODUCTS = {
    "framework-2.7": {
        "prd": "docs/prd/PRD-cognition.md",
        "criteria": ["AC%d" % n for n in range(1, 12)],
        "requirements": ["AC5 predicate definition, hits/false positives and >=7-day baseline",
                         "AC7 mechanism + pinned compatible seam fixture (dec-507)",
                         "M2-run scope and previous user-flow regression"],
    },
    "devkit-0.1": {
        "prd": "docs/prd/PRD-devkit.md",
        "criteria": ["DK-0.1-%02d" % n for n in range(1, 24)],
        "requirements": ["Each original PRD instance: consecutive 7 days after cutover; no staff substitution",
                         "source=pack/kit_dev/v0.1.0 on both instances",
                         "kit verb writes; kit index reads/rendering; paired agent-dev verdict"],
    },
    "agent-dev-0.1": {
        "prd": "docs/prd/PRD-agent-dev.md",
        "criteria": ["AD-0.1-%02d" % n for n in range(1, 19)],
        "requirements": ["Each original PRD instance: consecutive 7 days after cutover; no staff substitution",
                         "source=pack/agent_dev/v0.1.0 on both instances",
                         "Q1/Q2/Q3 conditions and paired devkit verdict"],
    },
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def timestamp(value):
    # The terminal adapter emits %z (+0900); Python 3.9 needs +09:00.
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs an explicit UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=True)
        stream.write("\n")


def annotation(body):
    values = MARK.findall(body)
    if len(values) > 1:
        raise ValueError("duplicate validation annotations")
    data = json.loads(values[0]) if values else {}
    if not isinstance(data, dict):
        raise ValueError("validation annotation must be an object")
    if data:
        if not all(isinstance(data.get(k), list) and
                   all(isinstance(v, str) for v in data[k]) for k in ("kinds", "states")):
            raise ValueError("kinds/states must be string lists")
        if not isinstance(data.get("synthetic", False), bool):
            raise ValueError("synthetic must be boolean")
        if not isinstance(data.get("disposition", {}), dict):
            raise ValueError("disposition must be an object")
        for key in ("runs", "proofs"):
            if not isinstance(data.get(key, []), list) or not all(
                    isinstance(v, dict) for v in data.get(key, [])):
                raise ValueError(key + " must be an object list")
        if not set(data.get("kinds", [])) or set(data["kinds"]) - KINDS:
            raise ValueError("kinds must contain execution and/or question")
        if not set(data.get("states", [])) or set(data["states"]) - STATES:
            raise ValueError("unknown or empty states")
        if not isinstance(data.get("outcome_known", True), bool):
            raise ValueError("outcome_known must be boolean")
        if not isinstance(data.get("input", {}), dict):
            raise ValueError("input must be an object")
        if set(data.get("input", {})) - INPUT_KEYS:
            raise ValueError("input contains a non-input field")
        if data.get("at"):
            if not isinstance(data["at"], str):
                raise ValueError("at must be an offset timestamp")
        for key in ("created_at", "outcome_at", "last_observed_at", "last_transition_at"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValueError(key + " must be a timestamp or null")
        for key in ("observed_at", "issues"):
            values = data.get(key, [])
            if not isinstance(values, list) or any(
                    not isinstance(value, str) and not (key == "observed_at" and value is None)
                    for value in values):
                raise ValueError(key + " must be a string list")
        for link in data.get("proofs", []):
            if not isinstance(link.get("evidence", []), list):
                raise ValueError("proof evidence must be a list")
        disposition = data.get("disposition", {})
        if not isinstance(disposition.get("done_evidence", []), list):
            raise ValueError("done evidence must be a list")
        if not isinstance(disposition.get("define", {}), dict):
            raise ValueError("carry-over define must be an object")
    return data


def source_time(value, local_tz=None):
    """No filesystem dates, coercion, or guessed offsets for event sources."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if local_tz is None:
            return timestamp(value)
        value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or local_tz).astimezone(dt.timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def markdown_observation(dl, bodies, ident, hi, local_tz):
    """Join same-ID mirrors; creation dates never supply a marker clock."""
    mapping = {"pending": "unanswered", "open": "unanswered", "confirmed": "answered",
               "answered": "answered", "resolved": "answered", "decided": "answered",
               "held": "held", "parked": "held", "rejected": "rejected",
               "not-executed": "not-executed", "cancelled": "cancelled",
               "\uD655\uC815": "answered"}
    claims, markers, created, status_dates = [], set(), [], []
    date_rx = r"\d{4}-\d\d-\d\d(?:[ T]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)?"
    for body in bodies:
        created_before = len(created)
        for line, fenced in dl._unfenced_lines(body):
            if fenced:
                continue
            marker = dl.fmt.MARK_RE.search(line)
            if marker and marker.group(1) == ident:
                markers.add(mapping[marker.group(2)])
            status = re.match(r"^(?:- )?status:\s*(.*)", line, re.I)
            if status:
                value = status[1].split()[0] if status[1].split() else ""
                state = mapping.get(value.lower().rstrip(".,"))
                if state:
                    date = re.search(date_rx, status[1])
                    if date:
                        status_dates.append(source_time(date[0][:10], local_tz))
                    # Reject a truncated clock instead of promoting its date to a
                    # precise answer time. Date-only statuses are explicit dates.
                    at = source_time(date[0], local_tz) if date else None
                    if date and re.match(r"[ T]\d", status[1][date.end():]):
                        at = None
                    claims.append((state, at))
            if re.match(r"^(?:- )?(?:Raised|opened):", line, re.I):
                date = re.search(date_rx, line)
                if date:
                    created.append(source_time(date[0], local_tz))
        if len(created) == created_before and body.splitlines():
            date = re.search(r"\d{4}-\d\d-\d\d", body.splitlines()[0])
            if date:
                created.append(source_time(date[0], local_tz))
    states, issues, observed = set(), [], []
    outcome = None
    for state in markers:
        # A marker can share a dated matching Status, never a creation date.
        if not claims or claims[-1][0] != state:
            claims.append((state, None))
    for state, at in claims:
        observed.append(at.isoformat() if at else None)
        if at is None:
            issues.append("markdown-state-at-cutoff-unknown")
            if state in {"unanswered", "held", "not-executed"}:
                states.add(state)
            continue
        # A dated future state does not erase recoverable past evidence.
        if at >= hi:
            continue
        states.add(state)
        if state in {"answered", "rejected", "cancelled"}:
            outcome = max(filter(None, (outcome, at)))
    return dict(states=states, issues=issues, observed_at=observed,
                created_at=min((a for a in created if a), default=None),
                first_observed_at=min((a for a in created + status_dates if a), default=None),
                outcome_at=outcome.isoformat() if outcome else None,
                last_transition_at=max((a for _, a in claims if a and a < hi), default=None),
                current_state=sorted(markers | {s for s, _ in claims}))


def inventory(dl, ledger, details, start, end, access_scope, asks=None, runs=None, kit_db=None):
    """Union index + closed detail records. Mirror copies count once.

    Conflicting/duplicate IDs, unreadable sources and missing dates are visible
    defects, never silent replacements or exclusions. Legacy text is not gold.
    """
    lo, hi = timestamp(start), timestamp(end)
    if hi <= lo or not access_scope.strip():
        raise ValueError("nonempty observation window and access scope required")
    sources, defects, grouped = {}, [], collections.defaultdict(list)

    def read(path):
        try:
            raw = Path(path).read_bytes()
            sources[str(path)] = hashlib.sha256(raw).hexdigest()
            return raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            defects.append({"source": str(path), "problem": type(exc).__name__})
            return None

    body = read(ledger)
    if body is not None:
        for ident, block in dl.decision_blocks(body):
            head = block.splitlines()[0]
            if not (re.match(r"^#+\s+" + re.escape(ident) + r"(?![\w])", head) or
                    re.match(r"^- \[\d{4}-\d\d-\d\d\] \[" + re.escape(ident) + r"\]", head)):
                continue
            full = re.search(r"(?<![\w-])" + re.escape(ident) + r"(?:-[\w-]+)?", block.splitlines()[0])
            grouped[full[0] if full else ident].append((str(ledger), block))
    if details:
        if not Path(details).is_dir():
            defects.append({"source": str(details), "problem": "details-unavailable"})
        else:
            rx = dl.id_regex(dl.conf("DECISION_LEDGER_ID_FORMAT"))
            for path in sorted(Path(details).glob("*.md")):
                if rx.fullmatch(path.stem):
                    body = read(path)
                    grouped[path.stem].append((str(path), body or ""))

    if asks:
        # Reuse the existing lifecycle fold without invoking policy/budget
        # checks or touching store globals. Read the explicitly supplied copy.
        from why_net.budget import fold_asks
        raw = read(asks)
        ask_groups = collections.defaultdict(list)
        for line_no, line in enumerate((raw or "").splitlines(), 1):
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or not isinstance(row.get("ask"), str)
                        or not row["ask"].strip()):
                    raise ValueError("missing ask ID")
                ask_groups[row["ask"]].append((line_no, row))
            except ValueError:
                defects.append({"source": str(asks), "line": line_no,
                                "problem": "unreadable-ask-row"})
        state_map = {"answered": "answered", "declined": "rejected",
                     "refused": "rejected", "ignored": "unanswered",
                     "parked": "held", "not_shown": "not-executed",
                     "moved": "discarded", "queued": "not-executed",
                     "create": "not-executed", "shown": "unanswered",
                     "awaiting": "unanswered"}
        for ident, numbered in sorted(ask_groups.items()):
            own_issues, past_rows = [], []
            numbered.sort(key=lambda item: (source_time(item[1].get("at")) or
                                           dt.datetime.max.replace(tzinfo=dt.timezone.utc), item[0]))
            for line_no, row in numbered:
                at = source_time(row.get("at"))
                if at is None:
                    own_issues.append("ask-time-unknown")
                    defects.append({"source": str(asks), "line": line_no, "id": ident,
                                    "problem": "ask-time-unknown"})
                if at is None or at < hi:
                    row = dict(row)
                    if not isinstance(row.get("ev"), str):
                        row["ev"] = "unknown"
                    past_rows.append(row)
            folded = fold_asks(past_rows).get(ident, {})
            payload = folded.get("validation") or {}
            if not isinstance(payload, dict):
                own_issues.append("invalid-ask-validation")
                payload = {}
            data = dict(payload)
            data.setdefault("at", numbered[0][1].get("at"))
            data["observed_at"] = [r.get("at") for _, r in numbered]
            data["last_observed_at"] = past_rows[-1].get("at") if past_rows else None
            state = folded.get("state")
            data["active"] = state not in ("answered", "declined", "refused", "moved")
            data["current_state"] = numbered[-1][1].get("ev")
            data["created_at"] = next((r.get("at") for r in past_rows if r.get("ev") == "create"), None)
            data["last_transition_at"] = data["last_observed_at"]
            data.setdefault("kinds", ["question"])
            if not isinstance(data.get("states", []), list):
                own_issues.append("invalid-ask-states")
                data["states"] = ["uncollected"]
            data["states"] = sorted(set(data.get("states", [])) |
                                    {state_map.get(state, "uncollected")})
            creates = sum(r.get("ev") == "create" for r in past_rows)
            if past_rows and creates != 1:
                own_issues.append("duplicate-ask-create" if creates else "missing-ask-create")
            if any(r.get("ev") not in state_map for r in past_rows):
                own_issues.append("unknown-ask-event")
            known = state in ("answered", "declined", "refused", "moved") and not own_issues
            data["outcome_at"] = past_rows[-1].get("at") if known else None
            data.setdefault("outcome_known", known)
            data["issues"] = own_issues
            grouped[ident].append((str(asks), "# " + ident + "\n<!-- decision-validation " +
                                   json.dumps(data, ensure_ascii=True) + " -->"))

    native_data = {}
    if runs or kit_db:
        from decision_validation_sources import terminal_records, kit_records
        aliases = {}
        for ident, copies in grouped.items():
            for _, body in copies:
                try:
                    for run in annotation(body).get("runs", []):
                        if run.get("id"):
                            if run["id"] in aliases and aliases[run["id"]] != ident:
                                raise ValueError("run linked to multiple judgments")
                            aliases[run["id"]] = ident
                except (ValueError, TypeError) as exc:
                    defects.append({"id": ident, "problem": str(exc)})
        if runs:
            raw = read(runs)
            extra, problems = terminal_records(raw or "", hi, timestamp)
            defects.extend(dict(problem, source=str(runs)) for problem in problems)
            for data in extra:
                ident = aliases.get(data["id"], data["id"])
                native_data.setdefault(ident, []).append(data)
                grouped[ident].append((str(runs), ""))
        if kit_db:
            try:
                sources[str(kit_db)] = hashlib.sha256(Path(kit_db).read_bytes()).hexdigest()
                for data in kit_records(kit_db, hi, timestamp):
                    native_data.setdefault(data["id"], []).append(data)
                    grouped[data["id"]].append((str(kit_db), ""))
            except (OSError, sqlite3.Error, ValueError) as exc:
                defects.append({"source": str(kit_db), "problem": type(exc).__name__})

    records, excluded = [], []
    for ident, copies in sorted(grouped.items()):
        issues, annotations = [], []
        if len({path for path, _ in copies}) != len(copies):
            issues.append("duplicate-id")
        for path, body in copies:
            try:
                data = annotation(body)
                if data:
                    annotations.append(data)
            except (ValueError, TypeError) as exc:
                issues.append("invalid-annotation: " + str(exc))
        if annotations and any(a != annotations[0] for a in annotations):
            issues.append("conflicting-mirrors")
        data = dict(annotations[0]) if annotations else {}
        annotation_states = set(data.get("states", []))
        state_at = source_time(data.get("outcome_at") or data.get("at"))
        if state_at is not None and state_at >= hi:
            data["states"] = []
            data["current_state"] = sorted(annotation_states)
            issues.append("annotation-state-at-cutoff-unknown")
        observations = list(native_data.get(ident, []))
        local_tz = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).tzinfo
        markdown = [markdown_observation(dl, [body for _, body in copies if body],
                                         ident, hi, local_tz)]
        for item in markdown:
            issues.extend(item["issues"])
            data["states"] = sorted(set(data.get("states", [])) | item["states"])
        for observed in observations:
            data.setdefault("at", observed.get("at"))
            for key in ("kinds", "states"):
                data[key] = sorted(set(data.get(key, [])) | set(observed[key]))
            data["runs"] = data.get("runs", []) + observed.get("runs", [])
            issues.extend(observed.get("issues", []))
        issues.extend(data.get("issues", []))
        instant = source_time(data.get("at"))
        if instant is None:
            instant = min((m["first_observed_at"] for m in markdown if m["first_observed_at"]), default=None)
        created_at = source_time(data.get("created_at", data.get("at"))) if annotations else min(
            [m["created_at"] for m in markdown if m["created_at"]] +
            [source_time(o.get("created_at")) for o in observations if source_time(o.get("created_at"))],
            default=None)
        creation = [instant] if instant else []
        if instant is None:
            issues.append("observation-time-unknown")
        observed_values = ([data.get("at"), data.get("last_observed_at"), data.get("outcome_at"),
                            data.get("last_transition_at")] +
                           data.get("observed_at", [])) if annotations else []
        for item in observations + markdown:
            observed_values.extend(item.get("observed_at", []))
            observed_values.extend([item.get("at"), item.get("last_observed_at")])
            transition = item.get("last_transition_at")
            observed_values.append(transition.isoformat() if isinstance(transition, dt.datetime) else transition)
        undated_state = any(None in item.get("observed_at", []) for item in [data] + observations + markdown)
        observed_values = [value for value in observed_values if value is not None]
        times = [source_time(value) for value in observed_values]
        times.extend(creation)
        if data.get("last_transition_at") is not None and source_time(data["last_transition_at"]) is None:
            issues.append("last-transition-time-unknown")
        # Undatable states/annotations cannot prove exclusion or a known result.
        unknown_time = bool(issues) or undated_state or any(t is None for t in times)
        if undated_state or any(t is None for t in times):
            issues.append("last-observation-time-unknown")
        past = [at for at in times if at is not None and at < hi]
        last_observed = max(past, default=None)
        outcomes = [source_time(item.get("outcome_at")) for item in [data] + observations + markdown]
        outcome_at = max((at for at in outcomes if at is not None and at < hi), default=None)
        transitions = [source_time(data.get("last_transition_at")), outcome_at]
        transitions.extend(o.get("last_transition_at") if isinstance(o.get("last_transition_at"), dt.datetime)
                           else source_time(o.get("last_transition_at")) for o in observations + markdown)
        last_transition = max((at for at in transitions if at and at < hi), default=None)
        requested_known = data.get("outcome_known", True) is True
        active = not outcome_at and bool(set(data.get("states", [])) & {"unanswered", "held", "not-executed"})
        active = active or any(o.get("active") for o in observations) or data.get("active") is True
        known = (bool(outcome_at) and requested_known and not active and not unknown_time
                 and (last_observed is None or outcome_at >= last_observed)
                 and (last_transition is None or outcome_at >= last_transition))
        if observations and not all(o.get("outcome_known") for o in observations):
            known = False
        # Exclude only positively dated nonmembers. Unknowns always survive.
        inside = any(lo <= at < hi for at in past)
        if (not past and instant is not None and instant >= hi and not undated_state
                and not any(t is None for t in times)):
            excluded.append({"id": ident, "reason": "created-after-window"})
            continue
        if not inside and not unknown_time and instant is not None:
            if last_observed is not None and last_observed < lo and known:
                excluded.append({"id": ident, "reason": "outside-window-without-unresolved-state"})
                continue
        membership = ("unknown" if instant is None else
                      "carry-in" if instant < lo and active else
                      "observed" if instant < lo else "created")
        # Outcome flags alone cannot remove an ID from the U denominator.
        reason = None if known else ("outcome-state-uncertain" if unknown_time or active
                                     else "dated-outcome-uncollected")
        current_states = [m["current_state"] for m in markdown if m["current_state"]]
        if data.get("current_state") is not None:
            current_states.append(data["current_state"])
        current_states.extend(o["current_state"] for o in observations if o.get("current_state") is not None)
        states = set(data.get("states", []))
        runs = data.get("runs", [])
        if any(r.get("status") == "failed" for r in runs):
            states.add("failed-run")
        safe_input = data.get("input", {})
        if (not safe_input or issues or data.get("outcome_known") is True and not known or not data or
                not all(isinstance(safe_input.get(k), str) and safe_input[k].strip()
                        for k in ("target", "version"))):
            states.add("uncollected")
        records.append({"id": ident, "at": instant.isoformat() if instant else "",
                        "window_membership": membership,
                        "kinds": sorted(set(data.get("kinds", ["question"]))),
                        "states": sorted(states), "input": safe_input,
                        "outcome_known": known, "outcome_at": outcome_at.isoformat() if outcome_at else None,
                        "outcome_unknown_reason": reason,
                        "created_at": created_at.isoformat() if created_at else None,
                        "last_observed_at": last_observed.isoformat() if last_observed else None,
                        "last_transition_at": last_transition.isoformat() if last_transition else None,
                        "synthetic": data.get("synthetic", False),
                        "runs": runs, "proofs": data.get("proofs", []),
                        "disposition": data.get("disposition", {}),
                        "issues": sorted(set(issues)), "current_state": current_states or None,
                        "sources": sorted({p for p, _ in copies})})
    return {"window": [start, end], "access_scope": access_scope,
            "sources": sources, "defects": defects, "excluded": excluded,
            "unidentified_observations_n": sum(d["problem"] == "unreadable-run-row" and
                                               "id" not in d for d in defects),
            "undated_ids": [r["id"] for r in records if not r["at"]],
            "records": sorted(records, key=lambda r: (r["at"], r["id"]))}


def sample(population, rule_version, seed=20261021):
    if not rule_version.strip():
        raise ValueError("rule version required")
    strata, chosen = {}, set()
    real = [r for r in population["records"] if not r["synthetic"]]
    performance = [r for r in real if not r["outcome_known"]]
    for label, kind in (("S1", "execution"), ("S2", "question")):
        for state in sorted(STATES):
            rows = [r for r in performance if kind in r["kinds"] and state in r["states"]]
            # Ordered population first, independent seeded draw for each stratum.
            rng = random.Random("%s:%s:%s" % (seed, label, state))
            selected = sorted(rng.sample(range(len(rows)), min(12, len(rows))))
            ids = [rows[i]["id"] for i in selected]
            chosen.update(ids)
            strata[label + "/" + state] = {
                "population_ids": [r["id"] for r in rows], "sample_ids": ids,
                "population_n": len(rows), "sample_n": len(ids),
                "shortfall": max(0, 12 - len(ids)), "excluded": [], "replaced": [],
                "uncollected_n": sum("uncollected" in r["states"] for r in rows)}
    selected = [r for r in performance if r["id"] in chosen]
    manifest = dict(population, seed=seed, rule_version=rule_version, strata=strata,
                    selected_ids=[r["id"] for r in selected],
                    diagnostic_ids=[r["id"] for r in real if r["outcome_known"]],
                    synthetic_ids=[r["id"] for r in population["records"] if r["synthetic"]])
    case_ids = {"case-" + digest([rule_version, population["window"], r["id"]]): r["id"]
                for r in selected}
    manifest["case_ids"] = case_ids
    manifest["counts"] = dict(observed=len(population["records"]), diagnostic=len(manifest["diagnostic_ids"]),
                              synthetic=len(manifest["synthetic_ids"]), performance=len(performance),
                              sampled=len(selected))
    native_to_case = {native: case for case, native in case_ids.items()}
    blind = {"rule_version": rule_version, "window": population["window"],
             "cases": [{"id": native_to_case[r["id"]], "input": r["input"]} for r in selected
                       if not r["outcome_known"] and "uncollected" not in r["states"]]}
    blind["packet_id"] = digest(blind)
    manifest["packet_id"] = blind["packet_id"]
    return manifest, blind


def assess(manifest, blind, judgments, now=utc_now):
    """Persist before reveal. No outcomes or selection reasons in this schema."""
    if digest({k: v for k, v in blind.items() if k != "packet_id"}) != blind["packet_id"]:
        raise ValueError("blind packet changed")
    if manifest["packet_id"] != blind["packet_id"]:
        raise ValueError("manifest does not match blind packet")
    expected = {c["id"] for c in blind["cases"]}
    if not isinstance(judgments, list) or len(judgments) != len(expected):
        raise ValueError("one assessment per blind case required")
    seen = set()
    for row in judgments:
        if set(row) != {"id", "ask_act", "decision_owner", "distance", "reason", "reviewer"}:
            raise ValueError("assessment fields required, outcomes forbidden")
        if row["id"] not in expected or row["id"] in seen:
            raise ValueError("unknown or duplicate assessment ID")
        seen.add(row["id"])
        if (row["ask_act"] not in ("ask", "act", "U") or
                not all(isinstance(row[k], str) and row[k].strip()
                        for k in ("decision_owner", "reason", "reviewer")) or
                not (row["distance"] == "unknown" or
                     type(row["distance"]) is int and row["distance"] >= 0)):
            raise ValueError("invalid pre-outcome judgment")
    return {"packet_id": blind["packet_id"], "manifest_id": digest(manifest),
            "rule_version": manifest["rule_version"], "recorded_at": now(),
            "judgments": judgments}


def evidence(ref):
    """A file/digest link is an observation, never a semantic acceptance claim."""
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        return False
    try:
        return hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() == ref["sha256"]
    except OSError:
        return False


def acceptance(records):
    products = {}
    for product, contract in PRODUCTS.items():
        links = collections.defaultdict(list)
        dispositions = []
        for row in records:
            for link in row["proofs"]:
                if link.get("product") != product:
                    continue
                valid = (link.get("criterion") in contract["criteria"] and
                         bool(link.get("evidence")) and
                         all(evidence(e) for e in link["evidence"]) and
                         bool(row["runs"]) and
                         all(r.get("id") and r.get("status") == "done" and
                             evidence(r.get("receipt")) for r in row["runs"]))
                links[link.get("criterion", "unknown")].append(
                    {"id": row["id"], "checked": valid, "link": link,
                     "runs": row["runs"]})
                disposition = row["disposition"]
                state = disposition.get("state", "unfinished")
                valid_disposition = False
                if state == "done":
                    valid_disposition = valid and bool(disposition.get("done_evidence")) and all(
                        evidence(e) for e in disposition["done_evidence"])
                elif state in ("cancel", "carry-over"):
                    valid_disposition = (evidence(disposition.get("owner_confirmation")) and
                                         bool(disposition.get("remaining_impact")))
                    if state == "carry-over":
                        define = disposition.get("define", {})
                        valid_disposition = valid_disposition and bool(disposition.get("destination")) and all(
                            define.get(k) for k in ("pointed_line", "how", "what", "done"))
                dispositions.append({"id": row["id"], "state": state,
                                     "supported": bool(valid_disposition),
                                     "counts_as_done": state == "done" and bool(valid_disposition),
                                     "detail": disposition})
        products[product] = dict(contract, evidence_by_criterion=dict(links),
                                 missing_criteria=[c for c in contract["criteria"]
                                                   if not any(l["checked"] for l in links[c])],
                                 dispositions=dispositions,
                                 unfinished_criteria=[c for c in contract["criteria"]
                                     if not any(d["counts_as_done"] and
                                         any(l["id"] == d["id"] and l["checked"] for l in links[c])
                                         for d in dispositions)],
                                 verdict="unfinished",
                                 proof_start="M2: not started by M1-8",
                                 acceptance="independent PRD gate required")
    return products


def reveal(manifest, assessment, outcomes, now=utc_now):
    if (assessment["packet_id"] != manifest["packet_id"] or
            assessment["manifest_id"] != digest(manifest) or
            assessment["rule_version"] != manifest["rule_version"]):
        raise ValueError("assessment/snapshot binding changed")
    if timestamp(assessment["recorded_at"]) > timestamp(now()):
        raise ValueError("assessment is in the future")
    selected = set(manifest["selected_ids"])
    rows = {r["id"]: r for r in manifest["records"]}
    judgments = {manifest["case_ids"][j["id"]]: j for j in assessment["judgments"]}
    golds = {}
    for outcome in outcomes:
        ident = outcome["id"]
        if ident not in selected or ident in golds:
            raise ValueError("unknown or duplicate outcome ID")
        if not timestamp(assessment["recorded_at"]) <= timestamp(outcome["recorded_at"]) <= timestamp(now()):
            raise ValueError("outcome must be opened after assessment")
        golds[ident] = outcome
    counts, input_defects = {}, {}
    for ident in selected:
        row, judgment, gold = rows[ident], judgments.get(ident), golds.get(ident, {})
        errors = []
        pairing = [key + "-mismatch" for key in ("target", "version")
                   if gold and gold.get(key) != row["input"].get(key)]
        input_defects[ident] = row["issues"] + pairing
        # An answer is never necessity gold; provenance + reviewer are mandatory.
        usable = (judgment and judgment["ask_act"] != "U" and
                  not row["outcome_known"] and "uncollected" not in row["states"] and
                  not input_defects[ident] and gold.get("gold_restorable") is True and
                  gold.get("reviewer") and gold.get("reason") and
                  gold.get("decision_owner") and
                  (gold.get("distance") == "unknown" or
                   type(gold.get("distance")) is int and gold["distance"] >= 0) and
                  evidence(gold.get("evidence")) and
                  all(type(gold.get(k)) is bool for k in
                      ("ask_needed", "act_needed", "asked", "acted")))
        if not usable:
            errors.append("U")
        else:
            if gold["decision_owner"] != judgment["decision_owner"]:
                errors.append("decision-owner-mismatch")
            if gold["distance"] != judgment["distance"]:
                errors.append("distance-mismatch")
            if gold["ask_needed"] and judgment["ask_act"] != "ask":
                errors.append("miss-question")
            if gold["act_needed"] and judgment["ask_act"] != "act":
                errors.append("miss-execution")
            if judgment["ask_act"] == "ask" and not gold["ask_needed"]:
                errors.append("over-ask")
            if judgment["ask_act"] == "act" and not gold["act_needed"]:
                errors.append("unnecessary-execution")
        counts[ident] = errors
    strata = {}
    for name, stratum in manifest["strata"].items():
        ids = stratum["sample_ids"]
        errors = collections.Counter(e for ident in ids for e in counts[ident])
        unknown = errors["U"]
        strata[name] = dict(stratum, denominator=len(ids), errors=dict(errors),
                            verdict=("indeterminate" if manifest["defects"] or not ids or unknown * 2 > len(ids)
                                     else "insufficient-sample" if stratum["shortfall"]
                                     else "measured"))
    return {"rule_version": manifest["rule_version"], "window": manifest["window"],
            "access_scope": manifest["access_scope"], "defects": manifest["defects"],
            "undated_ids": manifest["undated_ids"], "excluded": manifest["excluded"],
            "counts": manifest["counts"],
            "unidentified_observations_n": manifest["unidentified_observations_n"],
            "strata": strata, "unique_denominator": len(selected),
            "unique_errors": dict(collections.Counter(e for es in counts.values() for e in es)),
            "input_defects": input_defects,
            "case_errors": counts, "pre_assessment": assessment, "outcomes": outcomes,
            "S3_diagnostic_ids": manifest["diagnostic_ids"],
            "S4_synthetic_ids": manifest["synthetic_ids"],
            "dispositions": [{"id": r["id"], "detail": r["disposition"],
                              "runs": r["runs"], "proofs": r["proofs"]}
                             for r in manifest["records"] if r["disposition"]],
            "products": acceptance(manifest["records"]),
            "M2_complete": False, "M3_complete": False}


def cli(dl, argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    inventory_parser = sub.add_parser("inventory")
    snap = sub.add_parser("snapshot")
    for p in (inventory_parser, snap):
        p.add_argument("--ledger", required=True)
        p.add_argument("--details")
        p.add_argument("--asks", help="copy of existing Why-net asks.jsonl")
        p.add_argument("--runs", help="copy of existing terminal-state ledger")
        p.add_argument("--kit-db", help="read-only backup of existing kit SQLite store")
        p.add_argument("--start", required=True)
        p.add_argument("--end", required=True)
        p.add_argument("--access-scope", required=True)
    inventory_parser.add_argument("--state", choices=sorted(STATES))
    snap.add_argument("--rule-version", required=True)
    snap.add_argument("--seed", type=int, default=20261021)
    snap.add_argument("--out", required=True)
    pre = sub.add_parser("assess")
    pre.add_argument("--bundle", required=True)
    pre.add_argument("--judgments", required=True)
    post = sub.add_parser("reveal")
    post.add_argument("--bundle", required=True)
    post.add_argument("--outcomes", required=True)
    args = parser.parse_args(argv)
    try:
        if args.verb in ("inventory", "snapshot"):
            population = inventory(dl, args.ledger, args.details, args.start, args.end, args.access_scope, args.asks, args.runs, args.kit_db)
            if args.verb == "inventory":
                if args.state:
                    population["records"] = [r for r in population["records"] if args.state in r["states"]]
                print(json.dumps(population, indent=2, ensure_ascii=True))
            else:
                manifest, blind = sample(population, args.rule_version, args.seed)
                Path(args.out).mkdir(mode=0o700)
                write_new(Path(args.out) / "manifest.private.json", manifest)
                write_new(Path(args.out) / "blind.json", blind)
        else:
            bundle = Path(args.bundle)
            manifest = read_json(bundle / "manifest.private.json")
            if args.verb == "assess":
                result = assess(manifest, read_json(bundle / "blind.json"), read_json(args.judgments))
                write_new(bundle / "assessment.json", result)
            else:
                result = reveal(manifest, read_json(bundle / "assessment.json"), read_json(args.outcomes))
                write_new(bundle / "acceptance.json", result)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, "validation: %s\n" % exc)
    return 0
