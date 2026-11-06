"""User-customizable term registry over the layered i18n lookup (DGN-1980).

Owner directive (2026-10-10): every term the service hard-wires is
user-customizable from chat -- workout wording, internal-facing nouns (what
the user calls a subagent), the user's own form of address.  One mechanism:
a term is an i18n key, and the user's choice is a value in the DGN-1791
instance layer config/i18n.local/<lang>.json, so it beats every kit/pack
layer and the framework default and survives every update.

Kinds.  An i18n key is kind=term when a registry lists it (a short noun or
label); every other key is kind=sentence.  A sentence names a term with the
token [[term:<id>]]; i18n_lookup.lookup/merged_bundle (and the shell twin
agentlib.sh i18n) expand it, so every code emitter that reads copy through
the shared lookup switches with the term and never hard-wires the word.

Registry files (data, never code):
  framework   config/i18n/terms.json                  (template-shipped)
  kit/pack    config/packs/<pack-id>.i18n.terms.json  written by the same
              kit_i18n_layer.py sync from the payload's
              config/i18n/terms.bundle.json -- a pack tags its own terms.
Format "dogany-term-registry/1":
  {"terms": {"<id>": {"where": [...],
                      "synonyms": {"<lang>": [...]},
                      "examples": {"<lang>": ["... [[term:<id>]] ..."]},
                      "legacy_conf": "<agent.conf key>"}}}
  id matches [a-z][a-z0-9_]*; its i18n key is always "term.<id>".  A framework id
  is never redefined by a pack (first definition wins, framework first, then
  packs in file-name byte order).  The term's default VALUE lives in the
  normal i18n bundles under its key (framework <lang>.json / pack bundle).

Resolution of a user's term choice (resolve_term):
  instance layer (the user's chat choice)
  > legacy agent.conf knob (DASHBOARD_OWNER_TERM / DASHBOARD_SIBLING_TERM,
    pre-DGN-1980 instances keep rendering what they set)
  > kit/pack layers > framework default.

Fail-open everywhere: a missing/malformed registry file is skipped.
"""

import difflib
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import i18n_lookup  # noqa: E402

REGISTRY_FORMAT = "dogany-term-registry/1"
FRAMEWORK_REGISTRY = os.path.join("config", "i18n", "terms.json")
PACK_REGISTRY_GLOB = os.path.join("config", "packs", "*.i18n.terms.json")
TOKEN_RE = i18n_lookup.TERM_TOKEN_RE
ID_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
MAX_TERM_LEN = 40


def schema_labels(root, name):
    """Fixed record labels from framework term data, independent of locale.

    These are schema names, not customizable speaking terms. Translating or
    annotating them would change the four-slot grammar. Canonical routines
    use the same template registry that installation delivers to instances.
    """
    path = os.path.join(root, FRAMEWORK_REGISTRY)
    if not os.path.isfile(path) and os.path.isfile(os.path.join(root, ".dogany-canonical")):
        path = os.path.join(root, "agents", ".template", FRAMEWORK_REGISTRY)
    labels = _load(path).get("schemas", {}).get(name)
    if not isinstance(labels, dict) or not labels or not all(
            isinstance(v, str) and v.strip() for v in labels.values()):
        raise ValueError("schema-labels-unavailable")
    return dict(labels)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def registry_paths(root):
    packs = sorted(glob.glob(os.path.join(root, PACK_REGISTRY_GLOB)),
                   key=lambda p: os.path.basename(p).encode("utf-8"))
    return [os.path.join(root, FRAMEWORK_REGISTRY)] + packs


def load_registry(root):
    """id -> entry (with 'id', 'key', 'source' filled in)."""
    out = {}
    for path in registry_paths(root):
        terms = _load(path).get("terms")
        if not isinstance(terms, dict):
            continue
        source = "framework" if path.endswith(FRAMEWORK_REGISTRY) else \
            os.path.basename(path)[:-len(".i18n.terms.json")]
        for tid, entry in terms.items():
            if tid in out or not ID_RE.match(tid) or not isinstance(entry, dict):
                continue
            e = dict(entry)
            e["id"] = tid
            e["key"] = i18n_lookup.TERM_KEY_PREFIX + tid
            e["kind"] = "term"
            e["source"] = source
            out[tid] = e
    return out


def kind_of(root, key):
    """'term' when a registry lists the i18n key, else 'sentence'."""
    return "term" if any(e["key"] == key for e in load_registry(root).values()) \
        else "sentence"


# --------------------------------------------------------------------------
# values
# --------------------------------------------------------------------------
def _layers_raw(root, lang, key, with_instance):
    for lg in i18n_lookup.langs(lang or "en"):
        paths = i18n_lookup.layer_paths(root, lg)
        if not with_instance:
            paths = paths[1:]
        for path in paths:
            val = _load(path).get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def default_value(root, lang, entry):
    """The term's value WITHOUT the user's choice (kit/pack > framework)."""
    return _layers_raw(root, lang, entry["key"], with_instance=False)


def instance_value(root, lang, key):
    """The user's own choice for `key` in the instance layer ('' if none).
    Active language only: a choice made in one language never leaks."""
    val = _load(os.path.join(root, i18n_lookup.INSTANCE_DIR,
                             "%s.json" % (lang or "en"))).get(key)
    return val if isinstance(val, str) else ""


def conf_lang(root):
    """Active language: env DOGANY_LANG -> conf AGENT_LANG -> en (base code)."""
    lang = (os.environ.get("DOGANY_LANG") or "").strip() or \
        _conf(root, "AGENT_LANG")
    return re.split(r"[-_.]", lang.lower(), 1)[0] if lang else "en"


def _conf(root, key):
    try:
        from conf_reader import conf_get
    except Exception:
        return ""
    try:
        path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
            root, "config", "agent.conf")
        return (conf_get(key, path) or "").strip()
    except Exception:
        return ""


def resolve_term(root, lang, tid, registry=None):
    """Effective value of term `tid` (see module doc for the order)."""
    reg = registry if registry is not None else load_registry(root)
    entry = reg.get(tid)
    if entry is None:
        return ""
    val = instance_value(root, lang, entry["key"])
    if val:
        return val
    legacy = entry.get("legacy_conf")
    if legacy:
        val = _conf(root, legacy)
        if val:
            return val
    return default_value(root, lang, entry)


def expand(text, term_value):
    """Replace every [[term:<id>]] token via term_value(id); a term with no
    value keeps the token (visible, never a silent blank)."""
    if not isinstance(text, str) or "[[term:" not in text:
        return text

    def sub(m):
        val = term_value(m.group(1))
        return val if val else m.group(0)
    return TOKEN_RE.sub(sub, text)


# --------------------------------------------------------------------------
# phrase -> term id (deterministic)
# --------------------------------------------------------------------------
_QUOTES = "\"'`\u201c\u201d\u2018\u2019\u300c\u300d"
# Korean trailing particles tried when a phrase does not match as written
# (object / topic / subject markers).
_KO_PARTICLES = ("\uc744", "\ub97c", "\uc740", "\ub294", "\uc774", "\uac00")


def norm(s):
    s = (s or "").strip().strip(_QUOTES).strip().casefold()
    return re.sub(r"[\s_\-]+", "", s)


def _variants(phrase):
    p = (phrase or "").strip().strip(_QUOTES).strip()
    out = [p]
    for part in _KO_PARTICLES:
        if p.endswith(part) and len(p) > len(part):
            out.append(p[:-len(part)])
    if p.lower().endswith("s") and len(p) > 2:
        out.append(p[:-1])
    return [norm(v) for v in out if norm(v)]


def _names(root, entry):
    """Every surface form that names a term: id, key, values in every
    shipped language (default and current), synonyms."""
    names = {}
    langs = set()
    for path in [os.path.join(root, "config", "i18n")] + \
            [os.path.join(root, "config", "packs")]:
        for f in glob.glob(os.path.join(path, "*.json")):
            b = os.path.basename(f)
            if b.count(".") == 1:
                langs.add(b[:-5])
            elif ".i18n." in b:
                langs.add(b.rsplit(".i18n.", 1)[1][:-5])
    langs.discard("terms")
    for lg in sorted(langs | {"en"}):
        for v in (default_value(root, lg, entry),
                  instance_value(root, lg, entry["key"])):
            if v:
                names[v] = v
    syn = entry.get("synonyms")
    if isinstance(syn, dict):
        for vals in syn.values():
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str) and v.strip():
                        names[v] = v
    legacy = entry.get("legacy_conf")
    if legacy:
        v = _conf(root, legacy)
        if v:
            names[v] = v  # what the user sees today (pre-DGN-1980 knob)
    names.setdefault(entry["id"], entry["id"])
    names.setdefault(entry["key"], entry["key"])
    return names


def find_term(root, phrase, registry=None):
    """(status, ids, nearest):
      ('ok', [id], [])          exactly one term named by the phrase
      ('ambiguous', [ids], [])  the phrase names several terms
      ('unknown', [], [names])  no term; up to 3 nearest names (display)."""
    reg = registry if registry is not None else load_registry(root)
    table = {}  # normalized name -> set(ids)
    display = {}
    for tid, entry in reg.items():
        for raw in _names(root, entry).values():
            n = norm(raw)
            if n:
                table.setdefault(n, set()).add(tid)
                display.setdefault(n, raw)
    for v in _variants(phrase):
        ids = table.get(v)
        if ids:
            ids = sorted(ids)
            return ("ok", ids, []) if len(ids) == 1 else ("ambiguous", ids, [])
    target = norm(phrase)
    close = difflib.get_close_matches(target, sorted(table), n=3, cutoff=0.3)
    if not close:
        # substring fallback, deterministic order
        close = [n for n in sorted(table) if target and (target in n or n in target)][:3]
    return ("unknown", [], [display[n] for n in close])


# --------------------------------------------------------------------------
# utterance -> (old, new)
# --------------------------------------------------------------------------
_KO_SPLIT = re.compile(
    r"^\s*(?P<old>.+?)\s*(?:\ub9d0\uace0|\ub300\uc2e0\uc5d0|\ub300\uc2e0)\s+(?P<new>.+?)\s*$")
_KO_OBJ = re.compile(
    r"^\s*(?P<old>.+?)(?:\uc744|\ub97c)\s+(?P<new>.+?(?:\ub85c|\ub77c\uace0)"
    r"\s*(?:\ubc14\uafd4|\ubd88\ub7ec|\ud574|\uc368).*)$")
_KO_VERB_TAIL = re.compile(
    r"\s*(?:(?:\ud574|\ubd88\ub7ec|\uc368|\ubc14\uafd4)\s*"
    r"(?:\uc918|\uc8fc\uc138\uc694|\uc904\ub798\uc694|\uc904\ub798|\uc694)?"
    r"|\ud558\uc790|\ubd80\ub974\uc790|\uc4f0\uc790|\ubd80\ub97c\uac8c|\ud560\uac8c)\s*$")
_EN_RULES = (
    (re.compile(r"^(?:please\s+)?(?:say|use|write|call\s+it)\s+(?P<new>.+?)\s+"
                r"instead\s+of\s+(?P<old>.+?)$", re.I)),
    (re.compile(r"^(?:please\s+)?(?:rename|change)\s+(?P<old>.+?)\s+to\s+(?P<new>.+?)$", re.I)),
    (re.compile(r"^(?:please\s+)?replace\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)$", re.I)),
    (re.compile(r"^(?P<old>.+?)\s*(?:->|=>|\u2192)\s*(?P<new>.+?)$")),
)


def _has_batchim(ch):
    code = ord(ch) - 0xAC00
    return 0 <= code < 11172 and code % 28 != 0


def _is_hangul(ch):
    return 0 <= ord(ch) - 0xAC00 < 11172


def _strip_ko_quote(word):
    """'X(\uc774)\ub77c\uace0' / 'X(\uc73c)\ub85c' -> 'X', the vowel-harmony \uc774/\uc73c removed only
    when it is the particle (the preceding syllable has a final consonant,
    or is not Hangul at all)."""
    for full, short, link in (("\uc774\ub77c\uace0", "\ub77c\uace0", "\uc774"),
                              ("\uc73c\ub85c", "\ub85c", "\uc73c"),
                              ("\uc774\ub77c", "\ub77c", "\uc774")):
        if word.endswith(full) and len(word) > len(full):
            before = word[-len(full) - 1]
            if _has_batchim(before) or not _is_hangul(before):
                return word[:-len(full)]
        if word.endswith(short) and len(word) > len(short):
            return word[:-len(short)]
    return word


def _clean(s):
    return (s or "").strip().strip(_QUOTES).strip()


def parse_request(text):
    """Deterministic parse of a rename request -> (old, new) or None.
    ko: 'RIR \ub9d0\uace0 \uc5ec\uc720\ub77c\uace0 \ud574\uc918',
        'X \ub300\uc2e0 Y\ub85c \ubd88\ub7ec\uc918', 'X\ub97c Y\ub85c \ubc14\uafd4\uc918'
    en: 'say Y instead of X', 'rename X to Y', 'replace X with Y', 'X -> Y'."""
    t = re.sub(r"[.!?~\u3002]+\s*$", "", (text or "").strip())
    if not t:
        return None
    for rule in _EN_RULES:
        m = rule.match(t)
        if m:
            old, new = _clean(m.group("old")), _clean(m.group("new"))
            return (old, new) if old and new else None
    m = _KO_SPLIT.match(t) or _KO_OBJ.match(t)
    if not m:
        return None
    old = _clean(m.group("old"))
    new = _KO_VERB_TAIL.sub("", m.group("new")).strip()
    new = _clean(_strip_ko_quote(_clean(new)))
    return (old, new) if old and new else None


def valid_value(value):
    """'' when `value` is a usable term, else a reason code."""
    if not value or not value.strip():
        return "empty"
    if len(value) > MAX_TERM_LEN:
        return "too_long"
    if "\n" in value or "\r" in value:
        return "multiline"
    if any(c in value for c in "{}[]"):
        return "markup"
    return ""
