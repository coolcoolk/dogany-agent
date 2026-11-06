"""R3 start/scope/merge/external-apply judgment, using Why and host records.

Meaning, classification and estimates are explicitly PM judgments. Versions,
source hashes, graph distance and reservation receipts are observations. This
module never executes a payload, enables an OFF path, or sends a question.
"""
import json
import os
import subprocess
import unicodedata

from . import WhyRefusal, citations as C, events as E, nodes as N, store

STAGES = ("start", "scope", "merge", "external-apply")
MODES = ("self-build", "delegation", "recovery")
FLAGS = ("secret", "third_party_harm", "canon_conflict", "confirmed_line",
         "new_what", "public", "external_message", "budget_excess", "bot_control",
         "restart", "delete", "dev_artifact", "dev_apply", "ask_first",
         "irreversible", "delegated", "outage", "reversible_how")
# One table, evaluated in full. Later permission never erases an earlier floor.
TABLE = (
    ("T01", ("secret", "third_party_harm"), "forbid", "safety boundary"),
    ("T02", ("canon_conflict",), "forbid", "use the canonical approval path"),
    ("T03", ("confirmed_line",), "owner-confirm", "confirm this exact line/effect"),
    ("T04", ("new_what",), "owner-confirm", "present candidates for owner selection"),
    ("T05", ("public",), "owner-confirm", "request and confirm each public action"),
    ("T06", ("external_message", "budget_excess", "bot_control"), "owner-confirm", "use the explicit canonical approval path"),
    ("T07-T10", ("restart",), "off", "restart remains OFF; use current canon"),
    ("T11-T15", ("delete",), "off", "deletion remains OFF; preserve source and prove ownership/recovery/propagation"),
    ("T16", ("dev_artifact",), "allow", "build an internal artifact with candidate checks"),
    ("T17", ("dev_apply",), "off", "automatic apply/recommendation remains OFF"),
    ("T18", ("delegated", "outage", "reversible_how"), "allow", "only within previously allowed authority"),
    ("T20", ("ask_first",), "owner-confirm", "apply the case-specific correction immediately"),
)
RANK = {"allow": 0, "owner-confirm": 1, "supplement": 2, "off": 3, "forbid": 4}
UNKNOWN = ("", "unknown", "none", "tbd", "?", "make it work", "works well")


def known(value):
    return isinstance(value, str) and value.strip().lower() not in UNKNOWN


def digest(value):
    return store.sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def chain_errors(root=None):
    previous = store.GENESIS
    for raw in store._raw_lines(store.EVENTS, root):
        try:
            row = json.loads(raw)
        except ValueError:
            return True
        if not isinstance(row, dict) or row.get("prev") != previous:
            return True
        previous = store.sha256_text(raw)
    return False


def judgment_statement(data):
    """An exact owner-turn commitment, never a loose quotation reused as consent."""
    fields = {k: v for k, v in data.items() if k != "authority"}
    return "R3 judgment " + digest(fields)


def verified_judgment(event, root=None):
    """Re-resolve the real owner source every read; no stored confirmed string."""
    try:
        data = event["data"]
        auth = data["authority"]
        statement = judgment_statement(data)
        if event.get("ev") not in ("ticket-judgment", "r3-judgment") \
                or auth["quote"] != statement \
                or auth["quote_sha256"] != store.sha256_text(statement):
            return False
        # resolve uses the same source and injected-turn checks as confirm_quote.
        ref = E._resolve_or_refuse(statement, auth["session_id"], auth["message_uuid"])
        return ref.message_uuid == auth["message_uuid"] and ref.text.strip() == statement
    except (KeyError, TypeError, WhyRefusal, OSError, ValueError):
        return False


def record_judgment(data, session_id, message_uuid, root=None):
    """Append a bound judgment to the existing Why log, after owner verification.

    Caller first prepares the complete candidate (including PM basis and exact
    work/hashes), then pairs its statement with a real owner turn. No synthetic
    owner tap, auto-clear, or authority from repeated behavior exists here.
    """
    data = dict(data)
    data.pop("authority", None)
    if not all(known(data.get(k)) for k in
               ("ticket", "work_version", "before_sha256", "after_sha256", "basis", "reviewer")) \
            or data.get("verdict") not in ("same-meaning", "changed-meaning") \
            or data.get("uncertainty") != "none":
        raise WhyRefusal("judgment-incomplete")
    statement = judgment_statement(data)
    data["authority"] = dict(quote=statement, quote_sha256=store.sha256_text(statement),
                             session_id=session_id, message_uuid=message_uuid)
    with store.locked(root):
        rows = store.read_rows(store.EVENTS, root)
        work = next((e for e in rows if e["id"] == data["work_version"] and e.get("ev") == "work"), None)
        if not work or work["data"]["ticket"] != data["ticket"] \
                or work["data"]["ticket_sha256"] != data["before_sha256"] \
                or store.sha256_text(C._read_ticket(work["data"]["path"])) != data["after_sha256"]:
            raise WhyRefusal("judgment-version-mismatch")
        candidate = {"ev": "ticket-judgment", "data": data}
        if not verified_judgment(candidate, root):
            raise WhyRefusal("judgment-authority-missing")
        prior = next((e for e in rows if e.get("ev") == "ticket-judgment" and e["data"] == data), None)
        return prior or store.append_unlocked(store.EVENTS, lambda rs: E._event(
            rs, "ticket-judgment", None, "agent", data), root)


def ticket_judgment(work, before, after, root=None):
    rows = store.read_rows(store.EVENTS, root)
    # An invalid hash chain cannot supply an approval.
    if chain_errors(root):
        return None
    versions = [e["id"] for e in rows if e.get("ev") == "work" and e["data"] == work]
    for event in reversed(rows):
        d = event.get("data") or {}
        if event.get("ev") == "ticket-judgment" and d.get("ticket") == work["ticket"] \
                and d.get("work_version") in versions and d.get("before_sha256") == before \
                and d.get("after_sha256") == after and verified_judgment(event, root):
            return event if d.get("verdict") == "same-meaning" else None
    return None


def distance(work, rows, irreversible=False):
    """Shortest real purpose path; aliases/delegation add zero edges."""
    nodes = N.fold(rows)
    starts = [work["target"]] if work["target"] in nodes else work["relations"]["purpose"]
    C.reachable(nodes, starts)  # validates all branches, including cycles
    paths = []

    def visit(wid, path):
        path = path + [wid]
        if not nodes[wid]["parents"]:
            paths.append(path)
        for parent in nodes[wid]["parents"]:
            visit(parent, path)
    for wid in starts:
        visit(wid, [])
    if not paths:
        raise WhyRefusal("purpose-missing")
    path = min(paths, key=lambda p: (len(p), p))
    # A circle line itself begins at that line; an internal work cites its
    # registered purpose. The ticket->target alias contributes zero.
    raw = len(path) - (1 if work["target"] in nodes else 0)
    return dict(raw=raw, adjusted=max(0, raw - int(irreversible)),
                irreversible=irreversible, path=path,
                versions={w: C.versions(rows)[w] for w in path})


def _checkout_base():
    base = os.path.dirname(os.path.realpath(__file__))
    fallback = store._ROOT_DIR
    while not os.path.exists(os.path.join(base, ".git")):
        if os.path.isfile(os.path.join(base, "product.yaml")):
            fallback = base
        parent = os.path.dirname(base)
        if parent == base:
            return fallback
        base = parent
    return base


def _same(a, b):
    """Same directory by stat identity (case/normalisation-insensitive hosts)."""
    try:
        return a is not None and b is not None and os.path.samefile(a, b)
    except OSError:
        return False


def _repo_identity(top):
    """Common git dir shared by every worktree of one clone; None when unknown."""
    dot = os.path.join(top, ".git")
    try:
        if os.path.isdir(dot):
            return os.path.realpath(dot)
        with open(dot, encoding="utf-8") as stream:
            line = stream.read().strip()
        if not line.startswith("gitdir:"):
            return None
        gitdir = os.path.realpath(os.path.join(top, line[7:].strip()))
        if not os.path.isfile(os.path.join(gitdir, "commondir")):
            return gitdir  # a submodule or plain linked repo is its own common dir
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as stream:
            return os.path.realpath(os.path.join(gitdir, stream.read().strip()))
    except (OSError, UnicodeError):
        return None


def _owning_top(real):
    """Nearest enclosing directory holding .git: the repo that owns the path."""
    top = real
    while not os.path.lexists(os.path.join(top, ".git")):
        parent = os.path.dirname(top)
        if parent == top:
            return None
        top = parent
    return top


def _submodules(top):
    try:
        with open(os.path.join(top, ".gitmodules"), encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except (OSError, UnicodeError):
        return []
    return [_key_path(v.strip()) for k, _, v in (line.partition("=") for line in lines)
            if k.strip() == "path" and v.strip()]


def _key_path(rel):
    """Comparable spelling: '/'-separated, NFC, case-folded (APFS ignores both)."""
    rel = os.path.normpath(rel).replace(os.sep, "/")
    return unicodedata.normalize("NFC", rel).casefold()


_TAIL_MARKS = (":", "#", "\\", "@", "?")


def _spelling_refused(spelled, real, base_real):
    """A tail (line/anchor/URL/rev/backslash), padding or a diff prefix is never silently stripped.

    The key is built from the file, not from what was typed: a spelling that
    carries more than a path is refused rather than turned into a new key.
    """
    if spelled != spelled.strip() or any(ord(c) < 32 or ord(c) == 127 for c in spelled) \
            or any(m in spelled for m in _TAIL_MARKS):
        return True
    if os.path.lexists(real):
        return False
    # a/ or b/ in front of an existing path is a diff prefix, not a directory
    head, _, rest = os.path.normpath(spelled).partition(os.sep)
    return head in ("a", "b") and bool(rest) and os.path.lexists(os.path.join(base_real, rest))


def _inode(path):
    try:
        info = os.lstat(path)
        return info.st_dev, info.st_ino
    except OSError:
        return None


def _same_file_key(real, top, rel):
    """Hardlinks (one inode) share one key: the tracked name wins, else the least."""
    try:
        info = os.stat(real)
        if not os.path.isfile(real) or info.st_nlink < 2:
            return rel
        names = subprocess.run(["git", "-C", top, "ls-files", "-z", "-c", "-o", "--exclude-standard"],
                               capture_output=True, check=True, timeout=10).stdout.decode(
                                   "utf-8", "surrogateescape").split("\0")
        tracked = set(subprocess.run(["git", "-C", top, "ls-files", "-z"], capture_output=True,
                                     check=True, timeout=10).stdout.decode(
                                         "utf-8", "surrogateescape").split("\0"))
    except (OSError, subprocess.SubprocessError):
        return rel
    ident = (info.st_dev, info.st_ino)  # lstat: a symlink is not a second name of the inode
    same = [n for n in names if n and _inode(os.path.join(top, n)) == ident]
    if not same:
        return rel
    return sorted(same, key=lambda n: (n not in tracked, _key_path(n)))[0]


def map_location(spelled, base):
    """One spelling -> repo-relative key path of the gate repo, else refusal.

    The owning repo is the nearest enclosing .git, never a prefix shortcut;
    it must be the gate repo (same common git dir). Paths in a submodule,
    another clone or a nested repo, the repo root and escapes are invalid.
    """
    if not known(spelled):
        raise WhyRefusal("identity-location-invalid")
    base_real = os.path.realpath(base)
    real = os.path.realpath(os.path.join(base_real, spelled))
    if _spelling_refused(spelled, real, base_real):
        raise WhyRefusal("identity-location-invalid")
    top = _owning_top(real)
    inside_base = real == base_real or real.startswith(base_real + os.sep)
    if top is not None and (_same(top, base_real) or _same(
            _repo_identity(top), _repo_identity(base_real))):
        rel = _same_file_key(real, top, os.path.relpath(real, top))
    elif inside_base and (top is None or base_real.startswith(top + os.sep)):
        top, rel = base_real, os.path.relpath(real, base_real)  # gate tree without its own .git
    else:
        raise WhyRefusal("identity-location-invalid")
    key = _key_path(rel)
    if key in (".", "..") or key.startswith("../") or any(
            key == s or key.startswith(s + "/") for s in _submodules(top)):
        raise WhyRefusal("identity-location-invalid")
    return key


def location_paths(identity):
    """Every spelling of one location maps to one repo-relative key path."""
    base = _checkout_base()
    locations = identity.get("locations", [])
    if not isinstance(locations, list) or any(not known(p) for p in locations):
        raise WhyRefusal("identity-location-invalid")
    return {map_location(p, base) for p in locations}


def registration_identity(definition):
    """Stored on the work at registration; never re-resolved later.

    A dispatch worktree removed after the run, or a gate moved to an instance
    tree, cannot change the location a work registered (M1-3 R3-2).
    """
    identity = ((definition or {}).get("r3") or {}).get("identity") or {}
    if "locations" not in identity:
        return None
    try:
        paths = sorted(location_paths(identity))
    except WhyRefusal as exc:
        return dict(error=exc.reason)
    return dict(repo=_repo_identity(os.path.realpath(_checkout_base())), paths=paths)


def locations_overlap(a, b):
    """Same path, or one is a path-segment prefix of the other ("sample" !~ "sample2")."""
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in a for y in b)


def _work_paths(event):
    """(state, paths): ok / none (not an R3 work) / error (refused at registration) / legacy."""
    data = event["data"]
    stored = data.get("r3_identity")
    if stored is None:
        identity = (data["define"].get("r3") or {}).get("identity") or {}
        if not identity.get("locations"):
            return "none", set()
        try:
            return "ok", location_paths(identity)  # legacy record without a stored mapping
        except WhyRefusal:
            return "legacy", set()
    if stored.get("error") or not isinstance(stored.get("paths"), list):
        return "error", set()
    return "ok", set(stored["paths"])


def _effect_digest(define):
    values = [define.get(k) for k in ("effect_before", "effect_after", "done")]
    if not all(known(v) for v in values):
        return None
    return digest([unicodedata.normalize("NFC", " ".join(v.split())).casefold() for v in values])


def change_keys(event):
    """The atomic change keys of one work: location x effect digest."""
    effect = _effect_digest(event["data"]["define"])
    state, paths = _work_paths(event)
    return {(p, effect) for p in paths} if state == "ok" and effect else set()


def keys_match(a, b):
    return any(ea == eb and locations_overlap({pa}, {pb}) for pa, ea in a for pb, eb in b)


def change_group(seeds, rows):
    """Every registered work sharing a change key with the seeds, transitively.

    Links are hints that may only add seeds; they are never the identity.
    """
    works = [e for e in rows if e.get("ev") == "work"]
    keys = {e["id"]: change_keys(e) for e in works}
    selected = {e["id"] for e in seeds}
    group_keys = set().union(*(keys[i] for i in selected))
    added = True
    while added:
        added = False
        for e in works:
            if e["id"] not in selected and keys_match(keys[e["id"]], group_keys):
                selected.add(e["id"])
                group_keys |= keys[e["id"]]
                added = True
    return [e for e in works if e["id"] in selected]


def canonical_path(event, rows):
    """Keep every semantic snapshot and delegation hop on the identity path."""
    seen, lineage = set(), {}
    while True:
        if event["id"] in seen:
            raise WhyRefusal("target-alias-cycle")
        seen.add(event["id"])
        group = C.semantic_works(event["data"], rows)
        for member in group + [event]:
            lineage[member["id"]] = member
        identity = (event["data"]["define"].get("r3") or {}).get("identity") or {}
        linked = identity.get("canonical_work")
        if not linked:
            if group and group[0]["id"] != event["id"]:
                event = group[0]
                continue
            return event, list(lineage.values())
        prior = next((e for e in rows if e["id"] == linked and e.get("ev") == "work"), None)
        if not prior or linked not in identity.get("search", []):
            raise WhyRefusal("target-search-unlinked")
        # A link is a claim of sameness: it must share a change key.
        if not keys_match(change_keys(event), change_keys(prior)):
            raise WhyRefusal("target-effects-unclear")
        event = prior


def _recorded_flags(member):
    """Any flag a member records as anything but False restricts (sparse included)."""
    conditions = (member["data"]["define"].get("r3") or {}).get("conditions")
    if not isinstance(conditions, dict):
        return set()
    return {k for k in FLAGS if k in conditions and conditions[k] is not False}


def atom_judgment(atom, rows, root=None):
    event = next((e for e in rows if e.get("ev") == "work" and e["id"] == atom.get("work_version")), None)
    if not event:
        raise WhyRefusal("work-version-missing")
    work = event["data"]
    latest = C.registered_ticket(atom["path"], C.work_rows(rows))
    if latest is None or latest["id"] != event["id"]:
        raise WhyRefusal("work-version-stale")
    if not C._required(work.get("define"), C.DEFINE) or any(
            not known(work["define"].get(k)) for k in C.DEFINE):
        raise WhyRefusal("define-incomplete")
    evidence = work["define"].get("r3") or {}
    identity = evidence.get("identity") or {}
    if not C._required(identity, ("basis", "reviewer", "effect_before_evidence", "effect_after_evidence")) \
            or identity.get("uncertainty") != "none" \
            or not identity.get("locations") or not isinstance(identity.get("search"), list):
        raise WhyRefusal("identity-evidence-missing")
    state, own_paths = _work_paths(event)
    if state != "ok" or not own_paths:
        # A requester whose key set cannot be computed is never a new location.
        raise WhyRefusal("identity-location-invalid")
    own = C.semantic_works(work, rows)
    group = change_group([event] + own, rows)
    group_ids = {e["id"] for e in group}
    # Search is observed here, not accepted from a "no matches" assertion.
    # Only records predating this registration belong to its search snapshot.
    previous = rows[:rows.index(event)]
    findings, unmappable, candidates = [], [], []
    for old in previous:
        if old.get("ev") != "work" or old in own:
            continue
        old_state, old_paths = _work_paths(old)
        if old_state == "error":
            findings.append(dict(work=old["id"], finding="location-refused-at-registration"))
        elif old_state == "legacy":
            newest = C.work_rows(rows).get(old["data"]["ticket"])
            if newest is None or newest["id"] == old["id"] or newest["data"].get("r3_identity") is None:
                unmappable.append(old["id"])  # a newer stored mapping supersedes it: re-register frees it
        elif locations_overlap(own_paths, old_paths):
            candidates.append(old)
    if unmappable:
        # Fail closed and say which record; re-registering it stores its mapping.
        raise WhyRefusal("earlier-identity-unmappable:" + ",".join(unmappable))
    search = set(identity["search"])
    for old in candidates:
        members = group if old["id"] in group_ids else change_group([old], rows)
        if not search & {m["id"] for m in members if m in previous}:
            raise WhyRefusal("target-search-incomplete")
    if not identity.get("canonical_work") and not identity.get("searched_at"):
        raise WhyRefusal("target-search-missing")
    if not C.change_key(work["target"], work["define"]) or not change_keys(event):
        raise WhyRefusal("effect-unknown")
    _, lineage = canonical_path(event, rows)
    # Every hop retains its source checks and its complete classification.
    for member in lineage:
        if C.rechecks(member["data"], rows, root=root)[0]:
            raise WhyRefusal("recheck-required")
        conditions = (member["data"]["define"].get("r3") or {}).get("conditions") or {}
        if set(conditions) != set(FLAGS) or any(type(v) is not bool for v in conditions.values()):
            raise WhyRefusal("classification-incomplete")
    group = change_group(group + lineage, rows)
    group_ids = {e["id"] for e in group}
    # One change, one judgment: restrictions OR over the group, the closest
    # purpose wins, and which member a caller cites changes nothing.
    flags = set().union(*(_recorded_flags(m) for m in group))
    facts = {k: k in flags for k in FLAGS}
    nodes = N.fold(rows)
    if any(m["data"]["target"] in nodes for m in group):
        # The actual Why line is owner-owned regardless of a caller's label.
        facts["confirmed_line"] = True
    dist = min((distance(m["data"], rows, facts["irreversible"]) for m in group),
               key=lambda d: (d["adjusted"], d["raw"], d["path"]))
    canonical = group[0]
    neighbours = sorted(o["id"] for o in candidates if o["id"] not in group_ids)
    findings += [dict(work=w, finding="overlapping-location-different-effects") for w in neighbours]
    decisions = [{"row": row, "decision": dec, "reason": reason} for row, flags_, dec, reason in TABLE
                 if any(facts[k] for k in flags_)]
    if neighbours:
        # Not merged, not auto-different: the owner decides (plan R1-G), but a
        # neighbour's OFF/forbid row is never lowered to a question.
        notes = []
        for old in (o for o in candidates if o["id"] in neighbours):
            nflags = set().union(*(_recorded_flags(m) for m in change_group([old], rows)))
            shown = [r for r, fl, dec, why in TABLE if dec in ("off", "forbid") and nflags & set(fl)]
            decisions += [dict(row=r, decision=dec, reason=why + " (earlier work " + old["id"] + ")")
                          for r, fl, dec, why in TABLE if dec in ("off", "forbid") and nflags & set(fl)]
            notes.append(old["id"] + " flags=" + (",".join(sorted(nflags)) or "none")
                         + (" rows=" + ",".join(shown) if shown else ""))
            findings.append(dict(work=old["id"], finding="neighbour-restriction-flags", flags=sorted(nflags)))
        decisions.append(dict(row="R1-G", decision="owner-confirm",
                              reason="identity-unclear: earlier work at this location with different effects; "
                                     + "; ".join(notes)))
    # T18 cannot manufacture an exception to T19. A scoped delegation can
    # supply execution authority only after exact owner pairing below.
    decisions.append(dict(row="T19", decision="owner-confirm" if dist["adjusted"] < 2 else "allow",
                          reason="normalized purpose distance"))
    if facts["dev_artifact"]:
        candidate = evidence.get("candidate") or {}
        if not C._required(candidate, ("tree", "inputs", "impact_verdict", "smoke")) \
                or candidate["impact_verdict"] != "green" or candidate["smoke"] != "green":
            raise WhyRefusal("candidate-checks-missing")
    decision = max((d["decision"] for d in decisions), key=RANK.get)
    return dict(ticket=work["ticket"], work_version=event["id"], target=canonical["data"]["target"],
                canonical_work=canonical["id"], change_key=canonical["data"]["change_key"],
                change_keys=sorted(p + "#" + e for p, e in change_keys(event)),
                group=[m["id"] for m in group], findings=findings,
                distance=dist, decision=decision, final_authority="owner" if any(d["decision"] == "owner-confirm" for d in decisions) else "agent",
                rules=decisions, identity_judgment=identity,
                owner_confirmations={w: C.owner_confirmations(rows, root).get(w, dict(state="absent", record=None))
                                     for w in dist["path"]},
                allowed_scope=(canonical["data"]["define"].get("r3") or {}).get("scope", canonical["data"]["target"]))


def execution_binding(request, rows):
    binding = {k: request[k] for k in ("scope", "lane", "stage", "mode", "define", "atoms")}
    binding["operation"] = request.get("operation")
    binding["resources"] = request.get("resources")
    binding["baseline"] = request.get("baseline")
    binding["why_revision"] = digest([e for e in rows if e.get("ev") != "r3-judgment"])
    return binding


def record_authority(request, session_id, message_uuid, root=None):
    """Pair an exact execution request with a genuine owner confirmation."""
    with store.locked(root):
        rows = store.read_rows(store.EVENTS, root)
        data = dict(kind="execution-authority", input_sha256=digest(execution_binding(request, rows)),
                    judgment_id=request["judgment_id"], run_id=request["run_id"])
        statement = judgment_statement(data)
        data["authority"] = dict(quote=statement, quote_sha256=store.sha256_text(statement),
                                 session_id=session_id, message_uuid=message_uuid)
        if chain_errors(root) or not verified_judgment(dict(ev="r3-judgment", data=data), root):
            raise WhyRefusal("judgment-authority-missing")
        return store.append_unlocked(store.EVENTS, lambda rs: E._event(
            rs, "r3-judgment", None, "agent", data), root)


def evaluate(request, root=None):
    """Same fail-closed judgment for every caller and execution boundary.

    An allow result is executable only with the returned live reservation.
    A prior gate result is never accepted as current input.
    """
    result = dict(decision="supplement", reason="define-incomplete", atoms=[], reservation=None,
                  scope=request.get("scope"), lane=request.get("lane"),
                  next_check="before START/scope/lane/merge/external-apply and on any input change",
                  route="PM prepares evidence; owner decisions retain owner authority",
                  pass_path="supplement the missing evidence and re-run the same gate")
    try:
        if request.get("stage") not in STAGES or request.get("mode") not in MODES:
            raise WhyRefusal("gate-boundary-missing")
        if not C._required(request.get("define"), C.DEFINE) or any(
                not known(request["define"][k]) for k in C.DEFINE):
            raise WhyRefusal("define-incomplete")
        if not request.get("atoms"):
            raise WhyRefusal("target-missing")
        if not C._required(request, ("scope", "lane", "judgment_id", "run_id")):
            raise WhyRefusal("scope-run-missing")
        with store.locked(root):
            rows = store.read_rows(store.EVENTS, root)
            if chain_errors(root):
                raise WhyRefusal("why-store-integrity")
            atoms = [atom_judgment(a, rows, root) for a in request["atoms"]]
            result["atoms"] = atoms
            binding = execution_binding(request, rows)
            decision = max((a["decision"] for a in atoms), key=RANK.get)
            authority = next((e for e in rows if e["id"] == request.get("authority_record")), None)
            if decision == "owner-confirm" and authority:
                data = authority.get("data") or {}
                if data.get("kind") == "execution-authority" and data.get("input_sha256") == digest(binding) \
                        and data.get("judgment_id") == request["judgment_id"] \
                        and data.get("run_id") == request["run_id"] and verified_judgment(authority, root):
                    decision = "allow"
            if decision != "allow":
                result.update(decision=decision, reason="authority-boundary")
                return result
            allowed = ",".join(sorted({a["allowed_scope"] for a in atoms}))
            if request["scope"] != allowed:
                raise WhyRefusal("scope-not-defined")
            # Definition supplied at execution must be the registered one.
            for atom in request["atoms"]:
                work = next(e["data"] for e in rows if e["id"] == atom["work_version"])
                submitted = atom.get("define", request["define"])
                if any(submitted.get(k) != work["define"][k] for k in C.DEFINE):
                    raise WhyRefusal("define-version-mismatch")
            import host_dispatcher as hd
            bound_request = dict(request, atoms=[dict(a, ticket=b["ticket"]) for a, b in zip(request["atoms"], atoms)])
            receipt = hd.r3_reserve(bound_request, digest(binding))
            result.update(decision="allow" if receipt["state"] == "reserved" else "supplement",
                          reason=receipt["reason"], reservation=receipt,
                          evidence_version=binding["why_revision"])
            # Record the actual judgment and receipt in the existing event log.
            store.append_unlocked(store.EVENTS, lambda rs: E._event(rs, "r3-judgment", None, "agent",
                dict(judgment_id=request["judgment_id"], run_id=request["run_id"],
                     input_sha256=digest(binding), result=result)), root)
            return result
    except WhyRefusal as exc:
        result["reason"] = exc.reason
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["reason"] = "evidence-unavailable:" + type(exc).__name__
    except Exception as exc:
        # Host lock/storage errors never turn a comparison into a reservation.
        result["reason"] = "reservation-unavailable:" + type(exc).__name__
    return result


def is_registered(ticket, root):
    return ticket in C.work_rows(store.read_rows(store.EVENTS, root))


def registered_boundary(ticket, root, stage, request_path=None, operation=None, overrides=None):
    """Execution adapter for registered M1 work; legacy unregistered work is unchanged.

    Missing context on a registered ticket fails closed. A flag or alternate
    runtime cannot turn the registered ticket back into a legacy request.
    """
    rows = store.read_rows(store.EVENTS, root)
    work = C.work_rows(rows).get(ticket)
    if work is None:
        return None
    if not request_path or not os.path.isfile(request_path):
        raise WhyRefusal("r3-request-required")
    with open(request_path, encoding="utf-8") as stream:
        request = json.load(stream)
    if request.get("stage") != stage or request.get("operation") != operation:
        raise WhyRefusal("r3-operation-mismatch")
    if not any(a.get("work_version") == work["id"] and os.path.abspath(a.get("path", "")) == work["data"]["path"]
               for a in request.get("atoms", [])):
        raise WhyRefusal("r3-ticket-mismatch")
    request.update(overrides or {})
    result = evaluate(request, root)
    if result["decision"] != "allow":
        raise WhyRefusal("r3-" + result["decision"] + ":" + result["reason"])
    return result
