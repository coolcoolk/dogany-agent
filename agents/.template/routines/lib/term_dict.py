#!/usr/bin/env python3
"""Layered term dictionary loader (DGN-1500).

Owner lock (DGN-1500): dictionary units are framework / kit / module (no
skill unit). The dictionary is read by the send-time gate right before a
model message goes out; it is NEVER injected into model context.

Files (one per layer, each shipped by that layer's owner):
  framework  config/terms/framework.json     framework template (agents/
                                             .template/config, refreshed by
                                             update.sh like config/i18n)
  kit        config/packs/<pack-id>.terms.json   written by pack_install.sh
  module     config/packs/<pack-id>.terms.json   from the payload's
                                             config/terms.json; the writer
                                             (scripts/pack/lib/term_dict_layer.py)
                                             stamps layer=kit for kind=kit
                                             and layer=module for kind=pack
A module here is the domain unit an agent actually gets installed: a pack
that extends a kit (pack_health over kit_life). The kit-internal module
roster (kit manifest `modules`) is not per-agent switchable, so it cannot
scope an entry and is not a layer of its own.

Scoping: only files present on THIS instance are read, so an entry only
exists in an agent that has the layer that ships it. A kit/module layer can
also veto or replace a less specific entry:
  allow: [word, ...]      words this layer uses in their plain meaning
                          (health: the Korean word for
                          "legs" is also the literal "bridge"). A forbidden form equal to
                          an allowed word is dropped from every entry of a
                          LESS OR EQUALLY specific other layer.
  {"id": X, "off": true}  removes entry X of a less specific layer.
  {"id": X, ...}          replaces entry X of a less specific layer.

Resolution (most specific wins): module > kit > framework. Two layers of
the same rank that define the same id: the owner that sorts first (byte
order) wins; the collision is a conflict. Every override / veto / conflict
/ refused rewrite is reported once per distinct dictionary state (the
caller's `log` callback; the bridge adapter writes it to the output-gate
canary log).

Entry schema (format "dogany-term-dict/1"):
  id           [a-z0-9_.-]+, unique per layer
  forbidden    non-empty list of observed/expected mistranslations
  canonical    the owner's form (display only unless rewrite is honored)
  rewrite      true = safe-to-rewrite (see below); default false
  approved     who approved the canonical form and when (required for rewrite)
  context_any  optional cue words; a hit only counts when its own sentence
               also contains one of them (precision for ambiguous words)
  except       optional phrases that contain a form and are NOT a leak
               (an exercise name that starts with a forbidden loanword); a
               hit inside one of them is skipped
  note         free text
Rewrite is HONORED only when: canonical is set, approved is set, there is
no context_any (a context-gated form is ambiguous by definition), every
forbidden form is at least 2 characters, and the last syllable of each form
and of the canonical agree on a final consonant (so Korean particles stay
grammatical). Anything else is counted only, and the refusal is reported.
Even for an honored entry the matcher leaves a hit alone (counted only) when
its sentence already contains the canonical form: that sentence contrasts
the two spellings ("A, not B") and a rewrite would turn it into nonsense.

Fail direction: a missing, unreadable or invalid layer file disables the
WHOLE dictionary (load() returns None). Ignoring only the broken layer could
turn a veto off (a broken health layer would let the framework's "bridge"
mistranslation entry fire on "legs" in a health agent), so nothing is counted or rewritten until the
file is fixed. No files at all also returns None.

Pure stdlib, ASCII source; no bridge / harness imports.
"""

import glob
import json
import os
import re

FORMAT = "dogany-term-dict/1"
FRAMEWORK_REL = os.path.join("config", "terms", "framework.json")
PACK_DIR_REL = os.path.join("config", "packs")
PACK_SUFFIX = ".terms.json"
RANK = {"framework": 1, "kit": 2, "module": 3}
_ID_RE = re.compile(r"[a-z0-9_.-]+\Z")
_PACK_ID_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")


class DictError(ValueError):
    pass


def _is_hangul(ch):
    return "\uac00" <= ch <= "\ud7a3"


def _final_consonant(ch):
    """True/False for a Hangul syllable, None for anything else."""
    if not _is_hangul(ch):
        return None
    return (ord(ch) - 0xAC00) % 28 != 0


def _str_list(value, what, allow_empty=True):
    if value is None:
        return []
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() for v in value):
        raise DictError("%s must be a list of non-empty strings" % what)
    if not allow_empty and not value:
        raise DictError("%s must not be empty" % what)
    return [v.strip() for v in value]


def validate_layer(data, expect_layer=None, expect_owner=None):
    """Validate one parsed layer file; returns the normalized layer dict.

    Raises DictError. Shared with the pack-side writer so a payload that
    installs is a payload that loads.
    """
    if not isinstance(data, dict):
        raise DictError("layer is not a JSON object")
    if data.get("format") != FORMAT:
        raise DictError("format must be %r" % FORMAT)
    layer = data.get("layer")
    if expect_layer is not None and layer not in (
            (expect_layer,) if isinstance(expect_layer, str) else expect_layer):
        raise DictError("layer %r not allowed here" % (layer,))
    if layer not in RANK:
        raise DictError("unknown layer %r" % (layer,))
    owner = data.get("owner")
    if not isinstance(owner, str) or not _PACK_ID_RE.match(owner):
        raise DictError("owner must be a pack id / 'framework'")
    if expect_owner is not None and owner != expect_owner:
        raise DictError("owner %r does not match %r" % (owner, expect_owner))
    allow = _str_list(data.get("allow"), "allow")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise DictError("entries must be a list")
    seen = set()
    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise DictError("entry %d is not an object" % i)
        eid = e.get("id")
        if not isinstance(eid, str) or not _ID_RE.match(eid):
            raise DictError("entry %d: bad id %r" % (i, eid))
        if eid in seen:
            raise DictError("entry %r defined twice" % eid)
        seen.add(eid)
        if e.get("off") is True:
            out.append({"id": eid, "off": True})
            continue
        forbidden = _str_list(e.get("forbidden"), "entry %r forbidden" % eid,
                              allow_empty=False)
        canonical = e.get("canonical", "")
        if not isinstance(canonical, str):
            raise DictError("entry %r: canonical must be a string" % eid)
        rewrite = e.get("rewrite", False)
        if not isinstance(rewrite, bool):
            raise DictError("entry %r: rewrite must be true/false" % eid)
        approved = e.get("approved", "")
        if not isinstance(approved, str):
            raise DictError("entry %r: approved must be a string" % eid)
        out.append({
            "id": eid,
            "forbidden": forbidden,
            "canonical": canonical.strip(),
            "rewrite": rewrite,
            "approved": approved.strip(),
            "context_any": _str_list(e.get("context_any"),
                                     "entry %r context_any" % eid),
            "except": _str_list(e.get("except"), "entry %r except" % eid),
        })
    return {"layer": layer, "owner": owner, "allow": allow, "entries": out}


def rewrite_refusal(entry):
    """Why a rewrite:true entry is NOT safe to rewrite, or None if it is."""
    if not entry["canonical"]:
        return "no canonical form"
    if not entry["approved"]:
        return "no owner approval recorded"
    if entry["context_any"]:
        return "context-gated (ambiguous by definition)"
    for form in entry["forbidden"]:
        if len(form) < 2:
            return "form %r shorter than 2 characters" % form
        if form == entry["canonical"]:
            return "form %r equals the canonical" % form
        a, b = _final_consonant(form[-1]), _final_consonant(entry["canonical"][-1])
        if a is None and b is None:
            continue  # both non-Hangul endings: no particle agreement at stake
        if a is None or b is None or a != b:
            return ("form %r and canonical %r disagree on the final consonant "
                    "(particles would break)" % (form, entry["canonical"]))
    return None


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def layer_files(root):
    """[(path, expect_layer, expect_owner)] of every layer on this instance."""
    out = []
    fw = os.path.join(root, FRAMEWORK_REL)
    if os.path.exists(fw):
        out.append((fw, "framework", "framework"))
    pdir = os.path.join(root, PACK_DIR_REL)
    for p in sorted(glob.glob(os.path.join(glob.escape(pdir), "*" + PACK_SUFFIX)),
                    key=lambda x: os.path.basename(x).encode("utf-8")):
        pid = os.path.basename(p)[:-len(PACK_SUFFIX)]
        out.append((p, ("kit", "module"), pid))
    return out


def _fold(s):
    return s.casefold()


def resolve(layers, log=None):
    """Merge validated layers into rules; `log(kind, detail)` gets notes.

    Returns a list of rules, each:
      {id, layer, owner, canonical, forms, rewrite, context_any, except}
    """
    note = log or (lambda kind, detail: None)
    # lowest precedence first; within one rank the owner sorting FIRST is
    # processed last, so it wins (same tie-break as the i18n kit layers).
    order = sorted(layers, key=lambda l: l["owner"].encode(), reverse=True)
    order = sorted(order, key=lambda l: RANK[l["layer"]])
    resolved = {}
    for lay in order:
        for e in lay["entries"]:
            prev = resolved.get(e["id"])
            if prev is not None:
                kind = ("conflict" if RANK[prev["layer"]] == RANK[lay["layer"]]
                        else "override")
                note(kind, "id=%s %s/%s %s %s/%s" % (
                    e["id"], lay["layer"], lay["owner"],
                    "disables" if e.get("off") else "wins over",
                    prev["layer"], prev["owner"]))
            elif e.get("off"):
                note("override", "id=%s %s/%s disables an id no lower layer "
                     "defines" % (e["id"], lay["layer"], lay["owner"]))
            if e.get("off"):
                resolved.pop(e["id"], None)
                continue
            resolved[e["id"]] = dict(e, layer=lay["layer"], owner=lay["owner"])
    rules = []
    for eid in sorted(resolved):
        e = resolved[eid]
        forms = []
        for form in e["forbidden"]:
            vetoer = None
            for lay in layers:
                if (RANK[lay["layer"]] >= RANK[e["layer"]]
                        and _fold(form) in {_fold(a) for a in lay["allow"]}):
                    vetoer = lay
                    break
            if vetoer is not None:
                same = vetoer["owner"] == e["owner"]
                note("conflict" if same or RANK[vetoer["layer"]] == RANK[e["layer"]]
                     else "veto",
                     "id=%s form=%s from %s/%s allowed by %s/%s" % (
                         eid, form, e["layer"], e["owner"],
                         vetoer["layer"], vetoer["owner"]))
                continue
            forms.append(form)
        if not forms:
            continue
        rewrite = False
        if e["rewrite"]:
            why = rewrite_refusal(dict(e, forbidden=forms))
            if why is None:
                rewrite = True
            else:
                note("rewrite-refused", "id=%s %s/%s: %s -- counted only" % (
                    eid, e["layer"], e["owner"], why))
        rules.append({
            "id": eid, "layer": e["layer"], "owner": e["owner"],
            "canonical": e["canonical"], "forms": forms, "rewrite": rewrite,
            "context_any": list(e["context_any"]),
            "except": list(e["except"]),
        })
    return rules


_cache = {}


def _signature(files):
    sig = []
    for path, _, _ in files:
        try:
            st = os.stat(path)
            sig.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((path, None, None))
    return tuple(sig)


def load(root, log=None):
    """Resolved rules for the instance at `root`, or None (no/broken dictionary).

    Cached per root on the files' (mtime, size); `log` is called only when
    the dictionary is (re)built, so every note appears once per state.
    """
    root = str(root)
    files = layer_files(root)
    if not files:
        _cache.pop(root, None)
        return None
    sig = _signature(files)
    hit = _cache.get(root)
    if hit is not None and hit[0] == sig:
        return hit[1]
    note = log or (lambda kind, detail: None)
    layers = []
    rules = None
    try:
        for path, expect_layer, expect_owner in files:
            try:
                data = _read(path)
            except (OSError, ValueError) as e:
                raise DictError("%s unreadable (%s)" % (path, e))
            try:
                layers.append(validate_layer(data, expect_layer, expect_owner))
            except DictError as e:
                raise DictError("%s: %s" % (path, e))
        rules = resolve(layers, note)
    except DictError as e:
        note("broken", "%s -- whole dictionary disabled" % e)
        rules = None
    _cache[root] = (sig, rules)
    return rules


def _main(argv):
    """term_dict.py <instance-root>: print the resolved rules + load notes."""
    if len(argv) != 2:
        print("usage: term_dict.py <instance-root>")
        return 2
    rules = load(argv[1], log=lambda k, d: print("[%s] %s" % (k, d)))
    if rules is None:
        print("no dictionary (absent or broken)")
        return 1
    for r in rules:
        print(json.dumps(r, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
