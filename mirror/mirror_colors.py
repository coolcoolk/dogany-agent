"""DGN-849: kit-agnostic calendar color seam (config loader).

The framework ships the MECHANISM once; each kit/product declares its own
color axis in NON-vendored per-instance config -- the adapter never learns
any domain ontology (DGN-842: framework=machinery, kit=ontology). Config
lives at <instance>/config/calendar-colors.json (same non-vendored home as
config/i18n/, so self-update re-vendors of mirror/ never touch it).

Config schema (all keys optional; absent/invalid file == color seam OFF):

    {
      "color_source": "<event-dict field that drives the color>",
      "color_map":    {"<field value>": "<gcal colorId>", ...},
      "resolve": {
        "<derived field>": {"via":    "<event FK column>",
                             "table":  "<lookup table>",
                             "key":    "<lookup pk column>",
                             "select": "<lookup value column>"}
      }
    }

  - color_source names WHICH event field drives the color. Examples only
    (values live in kit config, never here): a life kit may color by an
    area's domain, a dev kit by project, a biz kit by client tier.
  - color_map maps that field's values (string-compared) to gcal colorIds.
  - resolve (optional) declares single-hop FK derivations so an axis that
    lives behind a foreign key is reachable WITHOUT adapter code changes:
    enrich_event attaches event[<derived field>] from
    SELECT <select> FROM <table> WHERE <key> = event[<via>]. All four
    identifiers must match ^[A-Za-z_][A-Za-z0-9_]*$ or the entry is dropped
    (config is owner-trusted, but a malformed spec must degrade to no-color,
    never to SQL breakage).

Subtype labels + status decorations (DGN-849 R&R): the framework owns the
title MECHANISM (a subtype label slot + a terminal-status decoration slot);
the TEXT is kit PRODUCT CONTENT and lives ONLY here in config, never in
framework code. Optional keys:

    {
      "subtype_fmt":    "<wrap with {label} and {title}>",   default "{label} {title}"
      "subtype_labels": {"<subtype key>": "<label text>", ...},
      "status_decor":   {"done": "<mark>", "expired": "<mark>", ...}
    }

Neutral degrade: a subtype with no config label renders with its raw
subtype KEY (a code value like presence/travel/prep), never a translated
word -- so a config-less instance emits NO domain/locale text. A status
with no declared decoration gets none. A kit that wants a human word or a
mark must publish it (Skull lane).

Fallback contract (zero-delta): no file / unparsable file / no color_source
-> active() is False and the adapter keeps its prior behavior byte-identically
(legacy status color markers included). Opt-in only.

English/ASCII only; domain VALUES live in the kit's JSON, never in code.
No external deps; the parse is cached (test seam: _reset_cache).
"""

import json
import os
import re

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_COLORS_PATH = os.path.normpath(
    os.path.join(_MODULE_DIR, "..", "config", "calendar-colors.json"))

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESOLVE_KEYS = ("via", "table", "key", "select")

# Neutral wrap when the kit declares no subtype_fmt: label then title,
# space-separated. Contains NO domain/locale text and NO styling punctuation
# beyond a separating space -- brackets/markers are kit styling choices.
_DEFAULT_SUBTYPE_FMT = "{label} {title}"

# GCal EVENT colorIds are '1'..'11' (surface constant of the adapter's own
# API, not domain data). A config typo outside this set is dropped to
# no-color instead of riding into the push body -- an invalid colorId would
# 400 every insert/update (400 is non-retryable) and stall that row's sync.
_VALID_COLOR_IDS = frozenset(str(n) for n in range(1, 12))

_CONFIG_CACHE = None    # parsed + validated config dict; {} when absent/bad


def _load():
    """Parsed config, cached. Missing/invalid file -> {} (seam off)."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        data = {}
        try:
            with open(_COLORS_PATH, "r") as fh:
                data = json.load(fh)
        except (OSError, IOError, ValueError):
            data = {}
        _CONFIG_CACHE = data if isinstance(data, dict) else {}
    return _CONFIG_CACHE


def _reset_cache():
    """Test seam: drop the cached parse so new config is re-read."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def color_source():
    """The declared color-source field name, or None (seam off)."""
    src = _load().get("color_source")
    if isinstance(src, str) and src.strip():
        return src.strip()
    return None


def active():
    """True when a kit color config is armed (color_source declared)."""
    return color_source() is not None


def subtype_fmt():
    """The title wrap format. Kit-declared 'subtype_fmt' when it is a string
    containing the {title} field; otherwise the neutral default (no domain
    text). Guarding on {title} prevents a malformed wrap from dropping the
    row title entirely."""
    fmt = _load().get("subtype_fmt")
    if isinstance(fmt, str) and "{title}" in fmt:
        return fmt
    return _DEFAULT_SUBTYPE_FMT


def subtype_label(subtype_key):
    """Kit-declared label TEXT for a subtype key, or the raw key itself as
    the neutral fallback (a code value, never a translated word). The
    framework holds no label text -- absence degrades to the key."""
    labels = _load().get("subtype_labels")
    if isinstance(labels, dict):
        v = labels.get(subtype_key)
        if isinstance(v, str) and v != "":
            return v
    return subtype_key


def subtype_label_values():
    """All kit-declared subtype label strings (inbound strip set). Empty
    when the kit declares none -- a config-less instance strips nothing."""
    labels = _load().get("subtype_labels")
    if not isinstance(labels, dict):
        return []
    return [v for v in labels.values() if isinstance(v, str) and v != ""]


def status_decor(status_key):
    """Kit-declared decoration TEXT for a terminal status, or None. The
    framework holds no decoration text -- absence means no decoration."""
    decor = _load().get("status_decor")
    if isinstance(decor, dict):
        v = decor.get(status_key)
        if isinstance(v, str) and v != "":
            return v
    return None


def status_decor_values():
    """All kit-declared status decoration strings (inbound strip set)."""
    decor = _load().get("status_decor")
    if not isinstance(decor, dict):
        return []
    return [v for v in decor.values() if isinstance(v, str) and v != ""]


def resolves():
    """Validated FK-derivation specs: {derived_field: {via,table,key,select}}.
    Entries with non-identifier names/columns are dropped wholesale --
    a bad spec can only cost color, never raise."""
    raw = _load().get("resolve")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for field, spec in raw.items():
        if not (isinstance(field, str) and _IDENT_RE.match(field)):
            continue
        if not isinstance(spec, dict):
            continue
        vals = {}
        for k in _RESOLVE_KEYS:
            v = spec.get(k)
            if not (isinstance(v, str) and _IDENT_RE.match(v)):
                vals = None
                break
            vals[k] = v
        if vals is not None:
            out[field] = vals
    return out


def color_for_event(event):
    """gcal colorId for this event per the declared axis, or None.
    Pure generic lookup: value of the color_source field (stringified)
    against color_map. Missing field / unmapped value / seam off -> None."""
    src = color_source()
    if src is None or not isinstance(event, dict):
        return None
    val = event.get(src)
    if val is None or val == "":
        return None
    cmap = _load().get("color_map")
    if not isinstance(cmap, dict):
        return None
    cid = cmap.get(str(val))
    if isinstance(cid, str) and cid.strip() in _VALID_COLOR_IDS:
        return cid.strip()
    return None
