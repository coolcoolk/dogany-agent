"""Layered i18n key lookup (DGN-1789) -- python side of agentlib.sh i18n().

Model (owner lock, dec-204): the framework ships keys + defaults in
config/i18n/<lang>.json; a kit overrides only the keys it needs in its OWN
layer; a key read returns the kit override if present, else the framework
default. A framework update rewrites config/i18n/<lang>.json and never
touches a kit layer, so a kit key survives every update.

Layers, per language (active lang first, then en):
  1. kit layers      config/packs/<pack-id>.i18n.<lang>.json, one file per
                     installed kit/pack, written by pack_install.sh from the
                     payload's config/i18n/<lang>.bundle.json. Between two
                     layers that override the same key the first wins, in
                     DEPENDENCY order (DGN-1789 r3): a pack that extends a
                     kit (manifest requires_kit) sits ABOVE that kit, so
                     the pack's override beats the kit's default (e.g.
                     pack_health over kit_life). Byte order of the file name
                     (LC_ALL=C) is only the tie-break among unrelated layers.
  2. framework       config/i18n/<lang>.json (framework-owned, refreshed by
                     update.sh).

Dependency record: config/packs/<pack-id>.i18n_order, written next to the
layer by the same writer (scripts/pack/lib/kit_i18n_layer.py) from the
pack manifest -- the instance has no manifest at lookup time. One
'<verb> <name>' per line:
  provides <kit>   this layer provides kit <kit> (a layer always provides
                   its own pack id as well)
  above <kit>      this layer sits above every layer that provides <kit>
Order algorithm: layers in file-name byte order; repeatedly emit the first
remaining layer that no other remaining layer sits above (a stable
topological sort, dependents first). A cycle emits what remains in name
order (deterministic, never an error). A missing/unreadable record means
"no dependency known" (name order); the writer warns about both at
install/update time, the lookup itself stays silent.
There is no instance-level override layer today. If one is added it goes
ABOVE the kit layers (instance > kit > framework default).

The shell twin is _i18n_files() in agentlib.sh; the two must list the same
files in the same order (tests/dgn1789_kit_i18n_layer_selftest.sh checks).

CONTRACT (DGN-1789, 2026-09-29): kit/pack code imports this module
from routines/lib/i18n_lookup.py and calls merged_bundle(root, lang) and
lookup(root, lang, key). A kit-shipped copy of mirror/mirror_i18n.py
(pack-owned mirror/) depends on it and silently degrades to config/i18n-only
if it moves. Path, names and signatures are a stable kit-facing contract:
change them only with a deprecation shim + kit owner notice.
tests/dgn1789_i18n_lookup_contract_selftest.py pins it.

Fail-open: a missing/unreadable/non-object file is skipped; a miss is ''.
CLI: i18n_lookup.py <instance-root> <lang> <key>  -> value ('' on miss).
"""

import glob
import json
import os
import re
import sys

KIT_LAYER_DIR = os.path.join("config", "packs")
KIT_LAYER_GLOB = "*.i18n.%s.json"
FRAMEWORK_DIR = os.path.join("config", "i18n")


ORDER_SUFFIX = ".i18n_order"
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")


def read_order(packs_dir, layer_id):
    """(provides, above, recorded) of one layer's dependency record.
    Shell twin: the record parse inside _i18n_files (agentlib.sh)."""
    provides, above = {layer_id}, set()
    try:
        with open(os.path.join(packs_dir, layer_id + ORDER_SUFFIX),
                  "r", encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except (OSError, ValueError):
        return provides, above, False
    for line in lines:
        parts = [p for p in re.split(r"[ \t]+", line) if p]
        if len(parts) != 2 or not _NAME_RE.match(parts[1]):
            continue
        if parts[0] == "provides":
            provides.add(parts[1])
        elif parts[0] == "above":
            above.add(parts[1])
    return provides, above, True


def order_layers(packs_dir, ids):
    """Dependency order of layer ids (see module doc). `ids` must already be
    in file-name byte order. Returns (ordered_ids, cycle)."""
    info = {i: read_order(packs_dir, i) for i in ids}
    remaining = list(ids)
    out = []
    while remaining:
        for x in remaining:
            if not any(y != x and info[y][1] & info[x][0] for y in remaining):
                out.append(x)
                remaining.remove(x)
                break
        else:
            return out + remaining, True
    return out, False


def layer_paths(root, lang):
    """Files consulted for one language, highest precedence first."""
    packs_dir = os.path.join(root, KIT_LAYER_DIR)
    suffix = ".i18n.%s.json" % lang
    kit = sorted(glob.glob(os.path.join(packs_dir, KIT_LAYER_GLOB % lang)),
                 key=lambda p: os.path.basename(p).encode("utf-8"))
    kit = [p for p in kit if os.path.isfile(p)]
    ids = [os.path.basename(p)[:-len(suffix)] for p in kit]
    ordered, _cycle = order_layers(packs_dir, ids)
    return [os.path.join(packs_dir, i + suffix) for i in ordered] + \
        [os.path.join(root, FRAMEWORK_DIR, "%s.json" % lang)]


def langs(lang):
    """Language ladder: active lang, then en (deduplicated)."""
    return [lang] if lang in ("", "en") else [lang, "en"]


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merged_bundle(root, lang):
    """One language's effective key->value map (kit layers over framework)."""
    merged = {}
    for path in reversed(layer_paths(root, lang)):
        merged.update(_load(path))
    return merged


def lookup(root, lang, key):
    """Effective value of `key`: per language, kit layers then framework
    default; '' when no layer of any language in the ladder has it."""
    for lg in langs(lang or "en"):
        for path in layer_paths(root, lg):
            val = _load(path).get(key)
            if isinstance(val, str) and val:
                return val
    return ""


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.stderr.write("usage: i18n_lookup.py <root> <lang> <key>\n")
        sys.exit(2)
    sys.stdout.write(lookup(sys.argv[1], sys.argv[2], sys.argv[3]))
