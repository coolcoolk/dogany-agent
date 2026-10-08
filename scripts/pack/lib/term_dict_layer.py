#!/usr/bin/env python3
"""term_dict_layer.py -- the ONE writer of a pack's term dictionary layer (DGN-1500).

The layered term dictionary (framework / kit / module) is read at send time
by routines/lib/term_dict.py. The framework layer ships in the template
(config/terms/framework.json); a kit or pack ships its own layer as
payload/config/terms.json, and this writer installs it as
config/packs/<pack-id>.terms.json, stamping:
  layer = "kit"     for kind=kit
  layer = "module"  for kind=pack (a pack is the domain module an agent gets)
  owner = <pack-id>
so the file on the instance says which layer it is (the instance has no
manifest at lookup time -- same reason as kit_i18n_layer.py's order record).

CLI:
  term_dict_layer.py sync <packs_dir> <pack_id> <payload_terms_json> <manifest>
    <payload_terms_json> present -> validated with term_dict.validate_layer
    (the loader's own check: a payload that installs is a payload that
    loads) and written atomically. Absent -> this pack's layer is removed
    (replace semantics: the payload no longer ships one).
    Invalid -> REFUSED, rc 1, previous layer kept.

Output: one '[term-dict-layer] ...' line per action (callers log verbatim).
Exit: 0 = done, 1 = refused, 2 = usage.
"""

import importlib.util
import json
import os
import sys
import tempfile

SUFFIX = ".terms.json"
LOADER_PY = os.environ.get("DOGANY_TERM_DICT_PY") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
    "agents", ".template", "routines", "lib", "term_dict.py")


def _loader():
    spec = importlib.util.spec_from_file_location("term_dict", LOADER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _kind(manifest_path):
    with open(manifest_path, encoding="utf-8") as f:
        d = json.load(f)
    kind = d.get("kind") if isinstance(d, dict) else None
    if kind not in ("kit", "pack"):
        raise ValueError("manifest kind %r is not kit|pack" % (kind,))
    return kind


def sync(packs_dir, pack_id, payload, manifest):
    layer_path = os.path.join(packs_dir, pack_id + SUFFIX)
    if not os.path.isfile(payload):
        if os.path.isfile(layer_path):
            os.remove(layer_path)
            print("[term-dict-layer] removed %s%s (payload ships no "
                  "config/terms.json)" % (pack_id, SUFFIX))
        return 0
    try:
        td = _loader()
        layer = "kit" if _kind(manifest) == "kit" else "module"
        with open(payload, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        data = dict(data, layer=layer, owner=pack_id)
        norm = td.validate_layer(data, layer, pack_id)
    except Exception as e:
        print("[term-dict-layer] REFUSED: %s (%s) -- previous layer kept"
              % (payload, e))
        return 1
    os.makedirs(packs_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=packs_dir, prefix=".terms-layer.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, layer_path)
    print("[term-dict-layer] %s%s: layer=%s, %d entr%s, %d allow" % (
        pack_id, SUFFIX, layer, len(norm["entries"]),
        "y" if len(norm["entries"]) == 1 else "ies", len(norm["allow"])))
    return 0


def main(argv):
    if len(argv) != 6 or argv[1] != "sync":
        print("usage: term_dict_layer.py sync <packs_dir> <pack_id> "
              "<payload_terms_json> <manifest>", file=sys.stderr)
        return 2
    return sync(argv[2], argv[3], argv[4], argv[5])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
