#!/usr/bin/env python3
"""base_contract -- the one validator for the manifest `base_contract` field
(DGN-2028 v2.7 merged design section 3, build step 1).

One optional, versioned manifest field declares what a kit's information
homes are: targets (what a slot is about), bindings (slot x target -> home),
read providers and workflow classes. This module is the ONLY implementation
of its shape check. Every install path records it through `record` below
(pack_install.sh _base_contract_record, both the kit and the pack call
sites), and the runtime reader `load_installed` re-runs the same `validate`
over the installed records, so an install and a turn can never disagree on
what a valid contract is.

Schema, install records and bounded per-turn source ownership discovery.
Source reads execute no provider. Provider adapters are checked for
existence and containment only; sources name installed local snapshots.

Verbs:
  base_contract.py record ROOT PACK_ID MANIFEST
      absent/null field removes any stale record; malformed or an adapter
      missing from the installed instance is an install error (rc 1, no
      partial write); else writes config/packs/<id>.base_contract.json
      (temp file + os.replace).
  base_contract.py check MANIFEST
      validate only (rc 0 valid or absent, 1 invalid); writes nothing.

Stdlib only; ASCII only (cue words are data inside the manifest).
"""
import json
import os
import re
import sys
import tempfile
import time
import stat

RECORD_SUFFIX = ".base_contract.json"
VERSION = 1
FIELD = "base_contract"

# Section 3: base slots. `procedure` / `profile` stay proposal destinations:
# a binding may name them, but never with write authority.
SLOTS = ("why", "decision", "problem", "work_unit", "target_state",
         "resource", "constraint", "goal", "explanation", "context")
PROPOSAL_SLOTS = ("procedure", "profile")
# DGN-2028 AMEND-2 2.9: slot names held for DGN-2120 (preference / state);
# a binding naming one is refused until that design lands.
RESERVED_SLOTS = ("preference", "state")
OWNERSHIPS = ("kit", "crew")
MODES = ("reference", "write", "none")
ON_MISSING = ("handoff", "hold")
OPERATIONS = ("lookup", "changes", "evidence")
STAKES = ("low", "normal", "high")
EVIDENCE_KINDS = ("unit",)
PROTOCOL = 1
# Single local problem-slot provider; no generic adapter registry.
PROBLEM_PROVIDER = ""  # An installed kit supplies its own provider path.
# A write-capable binding's provider must declare its held write door,
# capability probe and receipt lookup (section 3, provider protocol).
WRITE_KEYS = ("write_door", "capability_probe", "receipt_lookup")
# Why scope keeps its existing syntax (why_net/nodes.SCOPE_RE); a base
# resource name such as `base:cpu` is never a scope.
SCOPE_RE = re.compile(r"^(framework|kit:[A-Za-z0-9._-]+|product:[A-Za-z0-9._-]+)$")
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
LANG_RE = re.compile(r"^[a-z]{2}$")
TOP_KEYS = ("version", "defaults", "targets", "bindings", "providers", "workflows", "sources")
MAX_CUES = 32
MAX_CUE_LEN = 64
SOURCE_ORDER = ("skill", "module", "kit", "framework")
MAX_SOURCES = 32
MAX_HOMES = 128
SOURCE_BYTES = 8192
CONTEXT_BYTES = 64 * 1024


def problem_provider():
    """Return an explicitly installed provider; no ticket policy ships in core."""
    from record_provider import configured
    provider = configured()
    if provider is None:
        raise ValueError("problem-record-provider-unavailable")
    return provider.problem


def _cues(cues, where):
    if not isinstance(cues, dict) or not cues:
        _bad("%s: nonempty object of language -> cue list" % where)
    for lang, lst in cues.items():
        if not isinstance(lang, str) or not LANG_RE.fullmatch(lang):
            _bad("%s: invalid language key" % where)
        if (not isinstance(lst, list) or not lst or len(lst) > MAX_CUES
                or any(not isinstance(x, str) or not x.strip() or "\n" in x
                       or len(x) > MAX_CUE_LEN for x in lst)):
            _bad("%s: invalid cue list" % where)


class ContractError(ValueError):
    pass


def _bad(msg):
    raise ContractError(msg)


def _obj(v, where, allowed, required=()):
    if not isinstance(v, dict):
        _bad("%s: must be an object" % where)
    extra = sorted(set(v) - set(allowed))
    if extra:
        _bad("%s: unknown key(s) %s" % (where, ",".join(extra)))
    for k in required:
        if k not in v:
            _bad("%s: missing '%s'" % (where, k))
    return v


def _id(v, where):
    if not isinstance(v, str) or not ID_RE.match(v):
        _bad("%s: must match %s" % (where, ID_RE.pattern))
    return v


def _rel_path(v, where):
    """Instance-relative path: no absolute, `~`, empty, `.` or `..` segment."""
    if (not isinstance(v, str) or not v or v.startswith(("/", "~")) or "\\" in v
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)):
        _bad("%s: must be an instance-relative path" % where)
    if any(seg in ("", ".", "..") for seg in v.split("/")):
        _bad("%s: empty, '.' or '..' segment" % where)
    return v


def _unit_keys(manifest):
    """Declared base_units keys, or None when the manifest declares none."""
    bu = (manifest or {}).get("base_units")
    if not isinstance(bu, dict):
        return None
    keys = set()
    for u in bu.get("resource_units") or []:
        if isinstance(u, dict) and isinstance(u.get("key"), str):
            keys.add(u["key"])
    return keys


def validate(contract, manifest=None):
    """Raise ContractError on the first defect; return a list of warnings.

    `manifest` (optional) is the whole pack manifest, used to check that
    unit references name existing base_units keys. Without it, or when the
    manifest declares no base_units, unit references are shape-checked only
    and one warning says so."""
    warnings = []
    c = _obj(contract, FIELD, TOP_KEYS, ("version",))
    if c["version"] != VERSION or isinstance(c["version"], bool):
        _bad("%s.version: must be %d" % (FIELD, VERSION))
    units = _unit_keys(manifest)

    def unit_ref(v, where):
        if not isinstance(v, str) or not v.strip():
            _bad("%s: must be a nonblank string" % where)
        if v.startswith("base:"):
            _bad("%s: a base resource book cannot be referenced as a kit unit" % where)
        if units is not None and v not in units:
            _bad("%s: '%s' is not a declared base_units key" % (where, v))
        return v

    d = _obj(c.get("defaults", {}), FIELD + ".defaults", ("problem_line",))
    if "problem_line" in d and d["problem_line"] not in ("on", "off"):
        _bad("%s.defaults.problem_line: must be 'on' or 'off'" % FIELD)

    targets = c.get("targets", [])
    if not isinstance(targets, list):
        _bad("%s.targets: must be an array" % FIELD)
    tids = set()
    for i, t in enumerate(targets):
        w = "%s.targets[%d]" % (FIELD, i)
        _obj(t, w, ("id", "unit_ref", "why_scope", "cues"), ("id",))
        tid = _id(t["id"], w + ".id")
        if tid in tids:
            _bad("%s.id: duplicate target '%s'" % (w, tid))
        tids.add(tid)
        if "cues" in t:
            _cues(t["cues"], w + ".cues")
        if "unit_ref" in t:
            unit_ref(t["unit_ref"], w + ".unit_ref")
        if "why_scope" in t and (not isinstance(t["why_scope"], str)
                                 or not SCOPE_RE.match(t["why_scope"])):
            _bad("%s.why_scope: must match the Why scope syntax %s"
                 % (w, SCOPE_RE.pattern))

    providers = c.get("providers", {})
    if not isinstance(providers, dict):
        _bad("%s.providers: must be an object" % FIELD)
    for name, p in providers.items():
        w = "%s.providers.%s" % (FIELD, name)
        _id(name, w)
        _obj(p, w, ("adapter", "operations", "protocol") + WRITE_KEYS,
             ("adapter", "operations", "protocol"))
        _rel_path(p["adapter"], w + ".adapter")
        ops = p["operations"]
        if (not isinstance(ops, list) or not ops or len(set(ops)) != len(ops)
                or any(o not in OPERATIONS for o in ops)):
            _bad("%s.operations: nonempty distinct subset of %s"
                 % (w, ",".join(OPERATIONS)))
        if p["protocol"] != PROTOCOL or isinstance(p["protocol"], bool):
            _bad("%s.protocol: must be %d" % (w, PROTOCOL))
        for k in WRITE_KEYS:
            if k in p:
                _rel_path(p[k], "%s.%s" % (w, k))

    bindings = c.get("bindings", [])
    if not isinstance(bindings, list):
        _bad("%s.bindings: must be an array" % FIELD)
    homes = set()
    for i, b in enumerate(bindings):
        w = "%s.bindings[%d]" % (FIELD, i)
        _obj(b, w, ("slot", "target", "ownership", "mode", "provider", "on_missing"),
             ("slot", "target", "mode"))
        slot = b["slot"]
        if slot in RESERVED_SLOTS:
            _bad("%s.slot: '%s' is reserved for DGN-2120" % (w, slot))
        if slot not in SLOTS + PROPOSAL_SLOTS:
            _bad("%s.slot: '%s' is not a base slot" % (w, slot))
        if b["target"] not in tids:
            _bad("%s.target: '%s' is not a declared target" % (w, b["target"]))
        mode = b["mode"]
        if mode not in MODES:
            _bad("%s.mode: must be one of %s" % (w, ",".join(MODES)))
        key = (slot, b["target"])
        if key in homes:
            _bad("%s: second home for slot '%s' / target '%s' in one contract"
                 % (w, slot, b["target"]))
        homes.add(key)
        if mode == "none":
            # `none` withdraws this kit's claim; it carries no home.
            extra = sorted(set(b) & {"ownership", "provider", "on_missing"})
            if extra:
                _bad("%s: mode 'none' takes no %s" % (w, ",".join(extra)))
            continue
        if b.get("ownership") not in OWNERSHIPS:
            _bad("%s.ownership: must be one of %s" % (w, ",".join(OWNERSHIPS)))
        if b.get("provider") not in providers:
            _bad("%s.provider: must name a declared provider" % w)
        if b.get("on_missing", "handoff") not in ON_MISSING:
            _bad("%s.on_missing: must be one of %s" % (w, ",".join(ON_MISSING)))
        if mode == "write":
            if slot in PROPOSAL_SLOTS:
                _bad("%s: '%s' is a proposal destination; no write authority" % (w, slot))
            missing = [k for k in WRITE_KEYS if k not in providers[b["provider"]]]
            if missing:
                _bad("%s: write binding's provider lacks %s" % (w, ",".join(missing)))
        if "lookup" not in providers[b["provider"]]["operations"]:
            _bad("%s: provider '%s' must offer 'lookup'" % (w, b["provider"]))

    workflows = c.get("workflows", [])
    if not isinstance(workflows, list):
        _bad("%s.workflows: must be an array" % FIELD)
    wids = set()
    for i, wf in enumerate(workflows):
        w = "%s.workflows[%d]" % (FIELD, i)
        _obj(wf, w, ("id", "target", "cues", "stakes_hint", "evidence"),
             ("id", "target", "cues"))
        wid = _id(wf["id"], w + ".id")
        if wid in wids:
            _bad("%s.id: duplicate workflow '%s'" % (w, wid))
        wids.add(wid)
        if wf["target"] not in tids:
            _bad("%s.target: '%s' is not a declared target" % (w, wf["target"]))
        cues = wf["cues"]
        if not isinstance(cues, dict) or not cues:
            _bad("%s.cues: nonempty object of language -> cue list" % w)
        for lang, lst in cues.items():
            if not LANG_RE.match(lang):
                _bad("%s.cues: language key '%s' must match %s" % (w, lang, LANG_RE.pattern))
            if (not isinstance(lst, list) or not lst or len(lst) > MAX_CUES
                    or any(not isinstance(x, str) or not x.strip() or "\n" in x
                           or len(x) > MAX_CUE_LEN for x in lst)):
                _bad("%s.cues.%s: 1-%d nonblank one-line strings, <= %d chars"
                     % (w, lang, MAX_CUES, MAX_CUE_LEN))
        if wf.get("stakes_hint", "normal") not in STAKES:
            _bad("%s.stakes_hint: must be one of %s" % (w, ",".join(STAKES)))
        for j, ev in enumerate(wf.get("evidence", [])):
            ew = "%s.evidence[%d]" % (w, j)
            _obj(ev, ew, ("kind", "ref"), ("kind", "ref"))
            if ev["kind"] not in EVIDENCE_KINDS:
                _bad("%s.kind: must be one of %s" % (ew, ",".join(EVIDENCE_KINDS)))
            unit_ref(ev["ref"], ew + ".ref")
        if not isinstance(wf.get("evidence", []), list):
            _bad("%s.evidence: must be an array" % w)

    sources = c.get("sources", [])
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        _bad("%s.sources: array of at most %d sources" % (FIELD, MAX_SOURCES))
    sids = set()
    for i, source in enumerate(sources):
        w = "%s.sources[%d]" % (FIELD, i)
        _obj(source, w, ("id", "kind", "path", "target", "slots", "cues"),
             ("id", "kind", "path", "target"))
        sid = _id(source["id"], w + ".id")
        if sid in sids:
            _bad("%s: duplicate source id" % w)
        sids.add(sid)
        if source["kind"] not in SOURCE_ORDER + ("owner-file",):
            _bad("%s.kind: unknown source kind" % w)
        _rel_path(source["path"], w + ".path")
        if not isinstance(source["target"], str) or source["target"] not in tids:
            _bad("%s.target: must name a declared target" % w)
        slots = source.get("slots", [])
        if (not isinstance(slots, list) or any(s not in SLOTS + PROPOSAL_SLOTS for s in slots)
                or len(set(slots)) != len(slots)):
            _bad("%s.slots: distinct base slots" % w)
        if "cues" in source:
            _cues(source["cues"], w + ".cues")

    if units is None and (any("unit_ref" in t for t in targets)
                          or any(wf.get("evidence") for wf in workflows)):
        warnings.append("no base_units declared: unit references shape-checked only")
    return warnings


def _atomic_write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".base_contract.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(root, pack_id, manifest_path):
    """Install-side: write or remove config/packs/<id>.base_contract.json."""
    path = os.path.join(root, "config", "packs", pack_id + RECORD_SUFFIX)
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    val = manifest.get(FIELD)
    if val is None:
        if os.path.exists(path):
            os.remove(path)
        print("  base_contract: %s manifest declares none -- no record written" % pack_id)
        return 0
    try:
        warnings = validate(val, manifest)
        for name, p in val.get("providers", {}).items():
            for k in ("adapter",) + WRITE_KEYS:
                if k not in p:
                    continue
                full = os.path.realpath(os.path.join(root, p[k]))
                if not full.startswith(os.path.realpath(root) + os.sep):
                    _bad("providers.%s.%s: resolves outside the instance" % (name, k))
                if not os.path.isfile(full):
                    _bad("providers.%s.%s: '%s' not found in the installed instance"
                         " -- dead declaration" % (name, k, p[k]))
    except ContractError as e:
        print("base_contract: %s %s" % (pack_id, e), file=sys.stderr)
        return 1
    for w in warnings:
        print("  base_contract: %s WARN %s" % (pack_id, w))
    rec = {"pack_id": pack_id, FIELD: val}
    _atomic_write(path, (json.dumps(rec, indent=2, ensure_ascii=False, sort_keys=True)
                         + "\n").encode("utf-8"))
    print("  base_contract recorded: %s -> v%d targets=%d bindings=%d workflows=%d (%s)"
          % (pack_id, val["version"], len(val.get("targets", [])),
             len(val.get("bindings", [])), len(val.get("workflows", [])), path))
    return 0


def load_installed(root, max_files=32, max_bytes=256 * 1024, deadline=None,
                   outcomes=None):
    """Runtime reader: [(pack_id, contract)] for every installed record that
    still validates, sorted by pack id. Bounded local reads only; a missing
    directory, an oversize or invalid record is skipped (never raises)."""
    out = []
    d = os.path.join(root, "config", "packs")
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(RECORD_SUFFIX))
    except OSError:
        return out
    if outcomes is not None and len(names) > max_files:
        outcomes.append({"ref": "config/packs", "status": "file_limit"})
    for n in names[:max_files]:
        p = os.path.join(d, n)
        status = "unavailable"
        try:
            _id(n[:-len(RECORD_SUFFIX)], "installed pack id")
            if deadline is not None and time.monotonic() >= deadline:
                status = "deadline"
                continue
            raw, status = _read_local(root, os.path.relpath(p, root), max_bytes)
            if status != "loaded":
                continue
            rec = json.loads(raw)
            val = rec.get(FIELD)
            validate(val)
            status = "loaded"
        except (OSError, ValueError, AttributeError, TypeError):
            status = "invalid"
            continue
        finally:
            if outcomes is not None:
                outcomes.append({"ref": os.path.relpath(p, root), "status": status})
        out.append((n[:-len(RECORD_SUFFIX)], val))
    return out


def _read_local(root, ref, cap):
    """Contained regular files only; never block on a FIFO/device or execute code.

    Skill links are allowed when their resolved file is inside the instance.
    A crew snapshot must be delivered into the instance, like provider adapters.
    Oversize files are unavailable, never silently presented as complete.
    """
    full = os.path.realpath(os.path.join(root, ref))
    if not full.startswith(os.path.realpath(root) + os.sep):
        return "", "outside_instance"
    try:
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                return "", "not_regular"
            if info.st_size > cap:
                return "", "oversize"
            raw = fh.read(cap + 1)
        if len(raw) > cap:
            return "", "oversize"
        return raw.decode("utf-8"), "loaded"
    except (OSError, UnicodeError):
        return "", "unavailable"


def cue_hit(text, cues):
    """Declared aliases, independent of source prose similarity."""
    low = (text or "").casefold()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(c.casefold()) + r"(?![a-z0-9])", low)
               if c.isascii() else c.casefold() in low
               for words in (cues or {}).values() for c in words)


def resolve_sources(root, prompt, deadline, contracts=None):
    """One turn's ordered source reads and authoritative target/slot home map.

    Uses the existing installed-record discovery, never a parallel registry.
    A target's aliases/workflow cues select sources, including short prompts.
    Memory classification uses the same declarations, not text overlap. An
    unavailable home stays a home; overlaps remain explicit, with no winner.
    Providers are not executed on the hook path: declare a local snapshot.
    """
    discovery = []
    if contracts is None:
        contracts = load_installed(root, deadline=deadline, outcomes=discovery)
    sources, homes = [], []
    for pack, contract in contracts:
        for target in contract.get("targets", []):
            if len(homes) >= MAX_HOMES or time.monotonic() >= deadline:
                break
            tid = target["id"]
            aliases = {"id": [tid]}
            for lang, words in target.get("cues", {}).items():
                aliases.setdefault(lang, []).extend(words)
            for wf in contract.get("workflows", []):
                if wf["target"] == tid:
                    for lang, words in wf["cues"].items():
                        aliases.setdefault(lang, []).extend(words)
            declared = [s for s in contract.get("sources", []) if s["target"] == tid]
            for source in declared:
                for lang, words in source.get("cues", {}).items():
                    aliases.setdefault(lang, []).extend(words)
            active = cue_hit(prompt, aliases)
            for source in declared:
                if len(homes) >= MAX_HOMES:
                    break
                row = dict(source, pack=pack, ref=source["path"], status="not_selected")
                row["tier"] = "framework" if source["kind"] == "owner-file" else source["kind"]
                home = {"pack": pack, "target": tid, "slots": source.get("slots", []),
                        "kind": source["kind"], "ref": source["path"], "cues": aliases,
                        "source": row}
                homes.append(home)
                if active:
                    sources.append(row)
            # Existing provider homes without a snapshot remain unavailable,
            # rather than making memory the fallback authority.
            for binding in contract.get("bindings", []):
                if len(homes) >= MAX_HOMES:
                    break
                if binding["target"] != tid or binding["mode"] == "none":
                    continue
                if any(not s.get("slots") or binding["slot"] in s["slots"] for s in declared):
                    continue
                adapter = contract["providers"][binding["provider"]]["adapter"]
                row = {"id": binding["provider"], "pack": pack, "target": tid,
                       "kind": "kit", "tier": "kit", "ref": adapter,
                       "status": "snapshot_unavailable"}
                homes.append({"pack": pack, "target": tid, "slots": [binding["slot"]],
                              "kind": "kit", "ref": adapter, "cues": aliases, "source": row})
                if active:
                    sources.append(row)
    if len(homes) >= MAX_HOMES:
        discovery.append({"ref": "config/packs", "status": "ownership_limit"})
    # Always-active framework safety and owner files keep their existing
    # authority. The source ladder does not demote global safety rules.
    for ref in ("rules/hot.framework.md", "identity/hot.custom.agent.md",
                "identity/hot.custom.owner.md", "rules/hot.custom.md"):
        sources.append({"id": ref, "pack": "framework", "kind": "framework",
                        "tier": "framework", "ref": ref, "status": "pending"})
    sources.sort(key=lambda s: (SOURCE_ORDER.index(s["tier"]), s["pack"], s["id"]))
    remaining = CONTEXT_BYTES
    for i, row in enumerate(sources):
        if row["status"] == "snapshot_unavailable":
            continue
        if time.monotonic() >= deadline:
            row["status"] = "deadline"
        elif i >= MAX_SOURCES or remaining <= 0:
            row["status"] = "budget"
        else:
            text, status = _read_local(root, row["ref"], min(SOURCE_BYTES, remaining))
            row.update(status=status)
            if status == "loaded":
                row["text"] = text
                remaining -= len(text.encode("utf-8"))
    overlaps = []
    for i, home in enumerate(homes):
        for other in homes[i + 1:]:
            if (home["target"] == other["target"] and home["pack"] != other["pack"]
                    and (not home["slots"] or not other["slots"]
                         or set(home["slots"]) & set(other["slots"]))):
                overlap = {"target": home["target"],
                           "refs": sorted({home["ref"], other["ref"]})}
                if overlap not in overlaps and (home["source"] in sources or other["source"] in sources):
                    overlaps.append(overlap)
    return {"sources": sources, "homes": homes, "contracts": contracts,
            "discovery": discovery, "overlaps": overlaps}


def memory_home(item, resolved):
    """Resolve metadata or declared topic aliases, regardless of prose wording.

    Never infer a target merely from the current prompt: unrelated facts about
    the owner must survive. Unknown classification stays residual evidence.
    """
    text = item.get("text") or ""
    target = item.get("target")
    slot = item.get("slot")
    if not target:
        match = re.search(r"\btarget=([a-z][a-z0-9_-]*)", text)
        target = match.group(1) if match else None
    if not slot:
        match = re.search(r"\bslot=([a-z_]+)", text)
        slot = match.group(1) if match else None
    candidates = [h for h in (resolved or {}).get("homes", [])
                  if (target == h["target"] if target else cue_hit(text, h["cues"]))]
    matches = [h for h in candidates if not h["slots"] or slot in h["slots"]]
    if slot is None and ({(h["pack"], h["target"]) for h in candidates}
                         - {(h["pack"], h["target"]) for h in matches}):
        # A declared target with an unknown slot is not proof of no home.
        # In particular, an unavailable provider must not turn an untagged
        # paraphrase into authoritative memory by falling through this map.
        return {"status": "unresolved_slot", "refs": sorted({h["ref"] for h in candidates})}
    if not matches:
        return None
    packs = {h["pack"] for h in matches}
    targets = {h["target"] for h in matches}
    status = "ambiguous_target" if len(targets) > 1 else "overlap" if len(packs) > 1 else (
        "loaded" if any(h["source"]["status"] == "loaded" for h in matches) else "unavailable")
    return {"status": status, "refs": sorted({h["ref"] for h in matches})}


def source_refs(resolved):
    """Private frame ledger stores references/outcomes, never source contents."""
    return [{k: s[k] for k in ("kind", "pack", "ref", "status")}
            for s in resolved.get("sources", [])]


def render_sources(resolved, marker="[turn frame]"):
    lines = [marker + " source order: skill -> module -> kit -> framework -> memory; "
             "cite the owning source; memory is dated evidence. Global safety remains active."]
    for row in resolved.get("discovery", []):
        if row["status"] != "loaded":
            lines.append("%s contract ref=%s status=%s" % (marker, row["ref"], row["status"]))
    for source in resolved.get("sources", []):
        lines.append("%s source kind=%s pack=%s ref=%s status=%s" % (
            marker, source["kind"], source["pack"], source["ref"], source["status"]))
        for line in source.get("text", "").splitlines():
            lines.append("%s | %s" % (marker, line))
    for overlap in resolved.get("overlaps", []):
        lines.append("%s ownership overlap target=%s refs=%s; unresolved, no winner" % (
            marker, overlap["target"], ",".join(overlap["refs"])))
    return "\n".join(lines)


def main(argv):
    if len(argv) == 4 and argv[0] == "record":
        return record(argv[1], argv[2], argv[3])
    if len(argv) == 2 and argv[0] == "check":
        with open(argv[1], encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get(FIELD) is None:
            print("base_contract: absent")
            return 0
        try:
            for w in validate(manifest[FIELD], manifest):
                print("WARN %s" % w)
        except ContractError as e:
            print("base_contract: %s" % e, file=sys.stderr)
            return 1
        print("base_contract: valid")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
