"""DGN-2294 step 1: layered work-class resolver (routing as DATA, no launch).

Lookup order (docs/design/DGN-2294-work-classes.md):
  job or skill class reference -> framework definition (routines/work-classes)
  -> explicit kit override chain (config/packs/<kit>/work-classes) ->
  difficulty row -> ordered candidates.

Two halves:
  load_tree(...)  reads files into a Tree (the only I/O in this module).
  resolve(job, tree)  is a PURE function: same inputs -> same receipt,
                      never writes, launches or reaches the network.

Rules enforced here (each one rejects BEFORE anything is spent):
  - the caller never names a model/provider/place (work_class_schema);
  - class missing, unqualified name matching two classes, two files defining
    one class, or two kits overriding one class -> reject (no enumeration
    order ever picks a winner);
  - job class and skill delegate.work_class disagreeing -> reject;
  - kit classes live in their own namespace (<kit>.<name>); framework.* is
    framework-only;
  - scalars replace, a route row replaces as a whole, tools change only via
    explicit tools_add / tools_remove;
  - no layer may name a tool, place or model outside the framework guard
    set (_guard.md), and NEVER_GRANT tools are refused even if the guard file
    is edited to allow them;
  - every effective field records the source file + sha256 that set it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from work_class_schema import (CLASS_DEFINE_KEYS, CLASS_LISTS, CLASS_OVERRIDE_KEYS,
                               CLASS_SCALARS, CLASS_SCHEMA, DIFFICULTIES,
                               GUARD_SCHEMA, RECEIPT_SCHEMA, SchemaError,
                               validate_job)

# Floor that no data file can lift: a class or skill never grants
# baseline-edit, publication or notification authority.
NEVER_GRANT = frozenset({"baseline-edit", "publish", "notify", "merge",
                         "push-protected-ref", "owner-profile-edit"})
FRAMEWORK_NS = "framework"
GUARD_FILE = "_guard.md"
MAX_EXTENDS = 8

_KEY_RE = re.compile(r"^([a-z_][a-z0-9_.-]*):(?:[ ]+(.*))?$")
_SUB_RE = re.compile(r"^  ([a-z_][a-z0-9_.-]*):(?:[ ]+(.*))?$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9-]*$")
_CAND_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*)@([a-z][a-z0-9-]*)(?: if ([a-z][a-z0-9-]*))?$")


class ResolveError(SchemaError):
    """Resolution refused; nothing may be reserved or launched."""


# ---------------------------------------------------------------- parsing

def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scalar(raw, where):
    raw = raw.strip()
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise SchemaError("schema", f"{where}: unterminated list")
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part.strip(), where) for part in inner.split(",")]
    return _unquote(raw, where)


def _unquote(raw, where):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if not raw:
        raise SchemaError("schema", f"{where}: empty value")
    return raw


def parse_frontmatter(text, where):
    """Strict frontmatter subset: 'key: scalar', 'key: [a, b]', or 'key:' then
    two-space-indented 'sub: value' lines. Anything else is a schema error,
    so a typo can never be silently read as some other route."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SchemaError("schema", f"{where}: missing frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise SchemaError("schema", f"{where}: unterminated frontmatter") from None
    doc, block = {}, None
    for n, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            raise SchemaError("schema", f"{where}:{n}: tab in frontmatter")
        sub = _SUB_RE.match(line)
        if sub and block is not None:
            key, raw = sub.group(1), sub.group(2)
            if raw is None:
                raise SchemaError("schema", f"{where}:{n}: nesting deeper than one level")
            if key in doc[block]:
                raise SchemaError("schema", f"{where}:{n}: duplicate key {block}.{key}")
            doc[block][key] = _scalar(raw, f"{where}:{n}")
            continue
        top = _KEY_RE.match(line)
        if not top:
            raise SchemaError("schema", f"{where}:{n}: unreadable line")
        key, raw = top.group(1), top.group(2)
        if key in doc:
            raise SchemaError("schema", f"{where}:{n}: duplicate key {key}")
        if raw is None:
            doc[key], block = {}, key
        else:
            doc[key], block = _scalar(raw, f"{where}:{n}"), None
    return doc


def skill_delegate(text, where):
    """Read only the 'delegate:' block of a SKILL.md frontmatter (the rest of a
    skill's frontmatter is not this module's business). Returns {} if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise SchemaError("schema", f"{where}: unterminated frontmatter") from None
    body = lines[1:end]
    starts = [i for i, ln in enumerate(body) if ln.rstrip() == "delegate:"]
    if len(starts) > 1:
        raise SchemaError("conflict", f"{where}: two delegate blocks")
    if not starts:
        if any(ln.startswith("delegate") for ln in body):
            raise SchemaError("schema", f"{where}: delegate must be a block")
        return {}
    out = {}
    for ln in body[starts[0] + 1:]:
        if not ln.startswith("  "):
            break
        m = _SUB_RE.match(ln)
        if not m or m.group(2) is None:
            raise SchemaError("schema", f"{where}: unreadable delegate line")
        if m.group(1) in out:
            raise SchemaError("conflict", f"{where}: duplicate delegate.{m.group(1)}")
        out[m.group(1)] = _unquote(m.group(2).strip(), where)
    unknown = sorted(set(out) - {"work_class", "difficulty"})
    if unknown:
        raise SchemaError("schema", f"{where}: unknown delegate fields {', '.join(unknown)}")
    return out


# ---------------------------------------------------------------- loading

@dataclass
class Source:
    label: str          # repo-relative style label, never an absolute path
    sha256: str
    doc: dict


@dataclass
class Tree:
    guard: Source
    framework: list = field(default_factory=list)   # [Source]
    kits: dict = field(default_factory=dict)        # kit -> [Source]
    skill: Source | None = None


def _read(path, label, parse=parse_frontmatter):
    text = Path(path).read_text(encoding="utf-8")
    return Source(label, sha256_text(text), parse(text, label))


def load_tree(framework_dir, packs_dir=None, kits=None, skill_path=None,
              skill_label=None):
    """Read framework classes, the guard, installed kit files and an optional
    skill. kits=None means every installed kit takes part (conflicts between
    them reject); a list restricts the chain to those kits explicitly."""
    fdir = Path(framework_dir)
    guard_path = fdir / GUARD_FILE
    if not guard_path.is_file():
        raise ResolveError("missing", f"framework guard {GUARD_FILE} not found")
    tree = Tree(guard=_read(guard_path, f"framework/work-classes/{GUARD_FILE}"))
    for p in sorted(fdir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        tree.framework.append(_read(p, f"framework/work-classes/{p.name}"))
    if packs_dir is not None and Path(packs_dir).is_dir():
        installed = sorted(d.name for d in Path(packs_dir).iterdir()
                           if (d / "work-classes").is_dir())
        if kits is not None:
            missing = sorted(set(kits) - set(installed))
            if missing:
                raise ResolveError("missing", f"selected kit has no work-classes: {', '.join(missing)}")
            installed = [k for k in installed if k in kits]
        for kit in installed:
            tree.kits[kit] = [
                _read(p, f"kit/{kit}/work-classes/{p.name}")
                for p in sorted((Path(packs_dir) / kit / "work-classes").glob("*.md"))
                if not p.name.startswith("_")]
    elif kits:
        raise ResolveError("missing", "kits selected but no packs directory")
    if skill_path is not None:
        label = skill_label or f"skill/{Path(skill_path).parent.name}/{Path(skill_path).name}"
        tree.skill = _read(skill_path, label, parse=skill_delegate)
    return tree


# ---------------------------------------------------------------- guard

def _list(doc, key, where):
    val = doc.get(key, [])
    if not isinstance(val, list):
        raise SchemaError("schema", f"{where}: {key} must be a list")
    return val


def _guard(src):
    doc = src.doc
    if doc.get("schema") != GUARD_SCHEMA:
        raise SchemaError("schema", f"{src.label}: schema must be {GUARD_SCHEMA}")
    models = doc.get("models")
    if not isinstance(models, dict) or not models:
        raise SchemaError("schema", f"{src.label}: models block required")
    tools = set(_list(doc, "tools", src.label)) - NEVER_GRANT
    places = set(_list(doc, "places", src.label))
    if set(models) != places:
        raise SchemaError("schema", f"{src.label}: models must cover exactly the places")
    return {"tools": tools, "places": places,
            "models": {p: set(_scalar_list(v)) for p, v in models.items()},
            "predicates": set(_list(doc, "predicates", src.label))}


def _scalar_list(v):
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- classes

def _index(tree, guard):
    """name -> defining Source, and name -> overriding Source. Every file in
    the tree is checked, not only the ones on the requested path, so a broken
    data file fails every resolution instead of waiting to be hit."""
    defs, overrides = {}, {}

    def add_def(name, src):
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise SchemaError("schema", f"{src.label}: bad class name {name!r}")
        if name in defs:
            raise ResolveError("ambiguous",
                               f"class {name} defined twice: {defs[name].label}, {src.label}")
        defs[name] = src

    for src in tree.framework:
        _check_keys(src, define=True, guard=guard)
        name = src.doc.get("name")
        add_def(name, src)
        if not name.startswith(FRAMEWORK_NS + ".") or src.label.rsplit("/", 1)[1] != name.split(".", 1)[1] + ".md":
            raise SchemaError("schema", f"{src.label}: framework file must define framework.<file stem>")
        if "extends" in src.doc:
            raise SchemaError("schema", f"{src.label}: framework classes do not extend")
    for kit, srcs in tree.kits.items():
        for src in srcs:
            if "overrides" in src.doc:
                if "name" in src.doc:
                    raise SchemaError("schema", f"{src.label}: name and overrides are exclusive")
                _check_keys(src, define=False, guard=guard)
                overrides.setdefault(src.doc["overrides"], []).append((kit, src))
            else:
                _check_keys(src, define=True, guard=guard)
                name = src.doc.get("name", "")
                if not str(name).startswith(kit + "."):
                    raise SchemaError("schema",
                                      f"{src.label}: kit class must be named {kit}.<name>")
                if "extends" not in src.doc:
                    raise SchemaError("schema", f"{src.label}: kit class must extend a class")
                add_def(name, src)
    for target, items in overrides.items():
        if target not in defs:
            raise ResolveError("missing", f"{items[0][1].label}: overrides unknown class {target}")
        if len(items) > 1:
            raise ResolveError("ambiguous",
                               f"class {target} overridden by more than one file: "
                               + ", ".join(s.label for _, s in items)
                               + "; select one kit explicitly")
        kit, src = items[0]
        if target.split(".", 1)[0] not in (FRAMEWORK_NS, kit):
            raise SchemaError("schema",
                              f"{src.label}: a kit may override framework.* or its own classes only")
    return defs, {k: v[0][1] for k, v in overrides.items()}


def _check_keys(src, define, guard):
    allowed = CLASS_DEFINE_KEYS if define else CLASS_OVERRIDE_KEYS
    if src.doc.get("schema") != CLASS_SCHEMA:
        raise SchemaError("schema", f"{src.label}: schema must be {CLASS_SCHEMA}")
    unknown = sorted(set(src.doc) - set(allowed))
    if unknown:
        raise SchemaError("schema", f"{src.label}: unknown fields {', '.join(unknown)}")
    for key in ("tools", "tools_add"):
        if key in src.doc:
            _guard_tools(_list(src.doc, key, src.label), guard, src.label)
    extra = sorted(set(_list(src.doc, "place", src.label)) - guard["places"])
    if extra:
        raise ResolveError("authority", f"{src.label}: place outside guard set: {', '.join(extra)}")
    if "routes" in src.doc:
        routes = src.doc["routes"]
        if not isinstance(routes, dict):
            raise SchemaError("schema", f"{src.label}: routes must be a block")
        bad = sorted(set(routes) - set(DIFFICULTIES))
        if bad:
            raise SchemaError("schema", f"{src.label}: unknown difficulty rows {', '.join(bad)}")
        for diff, row in routes.items():
            if not isinstance(row, list) or not row:
                raise SchemaError("schema", f"{src.label}: routes.{diff} must be a non-empty list")
            for cand in row:
                _candidate(cand, guard, src.label)


def lookup(ref, defs):
    """Exact name, or an unqualified suffix matching exactly one class."""
    if ref in defs:
        return ref
    if "." in ref:
        raise ResolveError("missing", f"work class {ref} not found")
    hits = sorted(n for n in defs if n.split(".", 1)[1] == ref)
    if not hits:
        raise ResolveError("missing", f"work class {ref} not found")
    if len(hits) > 1:
        raise ResolveError("ambiguous", f"work class {ref} matches {', '.join(hits)}")
    return hits[0]


def _apply(eff, src, guard, override):
    """Layer one file's fields onto eff ({field: (value, Source)}). Guard
    checks already ran over the whole tree in _index."""
    doc, where = src.doc, src.label
    for key in CLASS_SCALARS:
        if key in doc:
            if not isinstance(doc[key], str):
                raise SchemaError("schema", f"{where}: {key} must be a scalar")
            eff[key] = (doc[key], src)
    for key in CLASS_LISTS:
        if key in doc:
            eff[key] = (_list(doc, key, where), src)
    if override:  # 'tools' is not an override key: only explicit add/remove
        tools = list(eff.get("tools", ([], None))[0])
        add = _list(doc, "tools_add", where)
        rem = _list(doc, "tools_remove", where)
        if add or rem:
            tools = [t for t in tools if t not in rem] + [t for t in add if t not in tools]
            eff["tools"] = (tools, src)
    elif "tools" in doc:
        eff["tools"] = (_list(doc, "tools", where), src)
    for diff, row in (doc.get("routes") or {}).items():
        eff["routes." + diff] = ([_candidate(c, guard, where) for c in row], src)


def _guard_tools(tools, guard, where):
    never = sorted(set(tools) & NEVER_GRANT)
    if never:
        raise ResolveError("authority", f"{where}: a class never grants {', '.join(never)}")
    extra = sorted(set(tools) - guard["tools"])
    if extra:
        raise ResolveError("authority", f"{where}: tools outside guard set: {', '.join(extra)}")


def _candidate(token, guard, where):
    m = _CAND_RE.match(token.strip()) if isinstance(token, str) else None
    if not m:
        raise SchemaError("schema", f"{where}: bad route candidate {token!r} "
                                    "(want <model>@<place>[ if <predicate>])")
    model, place, pred = m.groups()
    if place not in guard["places"]:
        raise ResolveError("authority", f"{where}: candidate place {place} outside guard set")
    if model not in guard["models"][place]:
        raise ResolveError("authority", f"{where}: model {model} not allowed at {place}")
    if pred is not None and pred not in guard["predicates"]:
        raise SchemaError("schema", f"{where}: unknown predicate {pred}")
    return {"model": model, "place": place, "when": pred,
            "effort": "provider-default"}


def _effective(name, defs, overrides, guard, seen=()):
    if name in seen or len(seen) > MAX_EXTENDS:
        raise ResolveError("schema", f"extends cycle or depth at {name}")
    src = defs[name]
    if "extends" in src.doc:
        parent = src.doc["extends"]
        if parent not in defs:
            raise ResolveError("missing", f"{src.label}: extends unknown class {parent}")
        eff = dict(_effective(parent, defs, overrides, guard, seen + (name,)))
    else:
        eff = {}
    _apply(eff, src, guard, override=False)
    if name in overrides:
        _apply(eff, overrides[name], guard, override=True)
    return eff


# ---------------------------------------------------------------- resolve

def resolve(job, tree):
    """Pure: Job + Tree -> receipt dict. Raises ResolveError/SchemaError."""
    validate_job(job)
    guard = _guard(tree.guard)
    defs, overrides = _index(tree, guard)
    delegate = tree.skill.doc if tree.skill else {}

    refs = []
    if job.get("class"):
        refs.append(("job", lookup(job["class"], defs)))
    if delegate.get("work_class"):
        refs.append((tree.skill.label, lookup(delegate["work_class"], defs)))
    if not refs:
        raise ResolveError("missing", "no work class: job.class or skill delegate.work_class required")
    if len({r for _, r in refs}) > 1:
        raise ResolveError("conflict", "job class and skill delegate.work_class disagree: "
                           + ", ".join(f"{w}={r}" for w, r in refs))
    name = refs[0][1]
    class_source = refs[0][0]

    difficulty = job.get("difficulty")
    diff_source = "job"
    if not difficulty and delegate.get("difficulty"):
        difficulty, diff_source = delegate["difficulty"], tree.skill.label
    if difficulty not in DIFFICULTIES:
        raise ResolveError("missing", f"difficulty must be one of {', '.join(DIFFICULTIES)}")

    eff = _effective(name, defs, overrides, guard)
    for need in ("covers", "tools", "place", "evidence"):
        if need not in eff:
            raise ResolveError("missing", f"class {name} has no {need}")
    row_key = "routes." + difficulty
    if row_key not in eff:
        raise ResolveError("missing", f"class {name} has no {difficulty} route row")
    row, row_src = eff[row_key]

    places = set(eff["place"][0])
    compute = job.get("compute") or {}
    forbidden = set(compute.get("forbidden_places", [])) | set(eff.get("forbidden_compute", ([], None))[0])
    facts = {"registered-command": bool(job.get("registered_command"))}
    candidates, dropped = [], []
    for cand in row:
        why = None
        if cand["place"] not in places:
            why = "place-not-in-class"
        elif cand["place"] in forbidden:
            why = "place-forbidden"
        elif cand["when"] and not facts.get(cand["when"], False):
            why = "predicate-false:" + cand["when"]
        (dropped if why else candidates).append(dict(cand, **({"dropped": why} if why else {})))
    if not candidates:
        raise ResolveError("no-candidate", f"class {name} {difficulty}: every candidate filtered")

    class_tools, tools_src = eff["tools"]
    authorized = set(job["authorization"]["tools"])
    granted = [t for t in class_tools if t in authorized and t in guard["tools"]
               and t not in NEVER_GRANT]
    withheld = [t for t in class_tools if t not in granted]

    def prov(src):
        return {"source": src.label, "sha256": src.sha256}

    fields = {
        "class": {"value": name, "source": class_source},
        "difficulty": {"value": difficulty, "source": diff_source},
        "covers": dict(value=eff["covers"][0], **prov(eff["covers"][1])),
        "evidence": dict(value=eff["evidence"][0], **prov(eff["evidence"][1])),
        "place": dict(value=sorted(places), **prov(eff["place"][1])),
        "tools": dict(value=class_tools, **prov(tools_src)),
        "route": dict(value=row_key, **prov(row_src)),
        "guard": prov(tree.guard),
    }
    for key in ("qualification", "required", "forbidden_compute"):
        if key in eff:
            fields[key] = dict(value=eff[key][0], **prov(eff[key][1]))
    if tree.skill:
        fields["skill"] = prov(tree.skill)

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "idempotency_key": job["idempotency_key"],
        "class": name,
        "difficulty": difficulty,
        "candidates": candidates,
        "dropped": dropped,
        "tools_granted": granted,
        "tools_withheld": withheld,
        "fields": fields,
        "launch": "none",
    }
    receipt["receipt_sha256"] = sha256_text(json.dumps(receipt, sort_keys=True,
                                                       ensure_ascii=True))
    return receipt
