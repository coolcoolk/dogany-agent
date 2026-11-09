#!/usr/bin/env python3
"""kit_i18n_layer.py -- the ONE writer of a pack's i18n override layer (DGN-1789).

Owner lock dec-204: the framework ships i18n keys + defaults in
config/i18n/<lang>.json; a kit overrides only the keys it needs in its OWN
layer, config/packs/<pack-id>.i18n.<lang>.json; the shared lookup
(agentlib.sh i18n() / routines/lib/i18n_lookup.py) returns the kit override
if present, else the framework default. Nothing here ever writes
config/i18n/.

Two callers, one writer (never fork this logic):
  * scripts/pack/pack_install.sh -- install / upgrade / reinstall, fed the
    payload's config/i18n directory.
  * update.sh (kit_i18n_layer_migrate) -- a framework-only update on an
    instance whose kit was installed under the old add-only merge model
    (no layer files yet), fed the same directory extracted from the
    INSTALLED pack version's tag (pack/<id>/v<installed>).

CLI:
  kit_i18n_layer.py sync <packs_dir> <pack_id> <bundle_dir> [<manifest>]
    Writes every <bundle_dir>/<lang>.bundle.json to
    <packs_dir>/<pack_id>.i18n.<lang>.json (replace, atomic per file) and
    removes this pack's layers for languages the bundle dir no longer ships.
    <bundle_dir> may be absent/empty: the pack then has no layer at all.
    Every bundle is validated BEFORE the first write; one bad bundle
    (unreadable, not a JSON object) refuses the whole
    sync with rc 1 and leaves every previous layer in place.
    With <manifest> (the pack's pack-manifest.json) the layer ORDER record
    is written too (see 'order'); then 'check' runs.
  kit_i18n_layer.py order <packs_dir> <pack_id> <manifest>
    Writes only the layer order record <packs_dir>/<pack_id>.i18n_order
    (DGN-1789 r3) from the manifest, then runs 'check'. update.sh uses it
    for a pack whose layer was written before the record existed.
  kit_i18n_layer.py check <packs_dir>
    Warns (never fails) about layers with no order record (name order) and
    about a dependency cycle (name order for the rest).
  DOGANY_KIT_I18N_DEFER_CHECK=1 skips the automatic 'check' after sync /
  order: a caller writing several packs in a row (update.sh) runs 'check'
  once at the end instead of warning about records it is about to write.

Layer order record (DGN-1789 r3): the lookup (routines/lib/i18n_lookup.py
layer_paths / agentlib.sh _i18n_files) puts a pack that extends a kit ABOVE
that kit, so e.g. pack_health's overrides of kit_life keys win. The
instance has no manifest at lookup time, so the dependency is recorded
here, derived from the manifest the install already validated:
  provides <provides_kit>        (kind=kit)
  above <requires_kit.kit>       (a pack extending a kit)
The record exists iff the pack has at least one layer. Grammar and order
algorithm: i18n_lookup.py module doc.

Output: one '[kit-i18n-layer] ...' line per action (callers log verbatim).
Exit: 0 = done (order problems are warnings), 1 = refused (bad bundle),
2 = usage.
"""

import glob
import importlib.util
import json
import os
import re
import sys
import tempfile

SUFFIX = '.bundle.json'
ORDER_SUFFIX = '.i18n_order'
_NAME_RE = re.compile(r'[A-Za-z0-9_.-]+\Z')
# The lookup whose order 'check' reports on: the framework copy shipped
# next to this writer (same repo tree), overridable for tests.
LOOKUP_PY = os.environ.get('DOGANY_I18N_LOOKUP_PY') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', '..',
    'agents', '.template', 'routines', 'lib', 'i18n_lookup.py')


def _load_bundles(bundle_dir):
    bundles = {}
    if not os.path.isdir(bundle_dir):
        return bundles, None
    for name in sorted(os.listdir(bundle_dir)):
        path = os.path.join(bundle_dir, name)
        if not name.endswith(SUFFIX) or not os.path.isfile(path):
            continue
        lang = name[:-len(SUFFIX)]
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return None, 'unreadable bundle %s (%s)' % (path, e)
        if not isinstance(data, dict):
            return None, 'bundle %s is not a JSON object' % path
        bundles[lang] = data
    return bundles, None


def _write(layer_path, data):
    d = os.path.dirname(layer_path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.i18n-layer.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, layer_path)


def sync(packs_dir, pack_id, bundle_dir):
    bundles, err = _load_bundles(bundle_dir)
    if err:
        print('[kit-i18n-layer] REFUSED: %s -- previous layers kept' % err)
        return 1
    for lang, data in sorted(bundles.items()):
        layer = os.path.join(packs_dir, '%s.i18n.%s.json' % (pack_id, lang))
        _write(layer, data)
        keys = sorted(data)
        print('[kit-i18n-layer] %s.i18n.%s.json: %d key(s): %s' % (
            pack_id, lang, len(keys),
            ', '.join(keys[:5]) + (' ...' if len(keys) > 5 else '')))
    prefix = '%s.i18n.' % pack_id
    for old in sorted(glob.glob(os.path.join(glob.escape(packs_dir),
                                             prefix + '*.json'))):
        lang = os.path.basename(old)[len(prefix):-len('.json')]
        if lang not in bundles and os.path.isfile(old):
            os.remove(old)
            print('[kit-i18n-layer] removed %s (language no longer shipped '
                  'by this pack)' % os.path.basename(old))
    return 0


def _layers(packs_dir, pack_id):
    prefix = '%s.i18n.' % pack_id
    return sorted(p for p in glob.glob(os.path.join(
        glob.escape(packs_dir), prefix + '*.json')) if os.path.isfile(p))


def _order_lines(manifest_path):
    """Record lines derived from a pack manifest, or (None, reason)."""
    try:
        with open(manifest_path, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return None, 'unreadable manifest %s (%s)' % (manifest_path, e)
    if not isinstance(d, dict):
        return None, 'manifest %s is not a JSON object' % manifest_path
    lines = []
    pk = d.get('provides_kit')
    if isinstance(pk, str) and _NAME_RE.match(pk):
        lines.append('provides %s' % pk)
    rk = d.get('requires_kit')
    kit = rk.get('kit') if isinstance(rk, dict) else None
    if isinstance(kit, str) and _NAME_RE.match(kit):
        lines.append('above %s' % kit)
    return lines, None


def write_order(packs_dir, pack_id, manifest_path):
    rec = os.path.join(packs_dir, pack_id + ORDER_SUFFIX)
    if not _layers(packs_dir, pack_id):
        if os.path.isfile(rec):
            os.remove(rec)
            print('[kit-i18n-layer] removed %s%s (pack has no layer)'
                  % (pack_id, ORDER_SUFFIX))
        return
    lines, err = _order_lines(manifest_path)
    if err:
        if os.path.isfile(rec):
            os.remove(rec)
        print('[kit-i18n-layer] WARN: %s: no layer order record (%s) -- '
              'this layer falls back to name order' % (pack_id, err))
        return
    body = ('# DGN-1789 i18n layer order, derived from pack-manifest.json '
            'by kit_i18n_layer.py -- do not edit\n'
            + ''.join(l + '\n' for l in lines))
    d = os.path.dirname(rec)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.i18n-order.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, rec)
    print('[kit-i18n-layer] %s%s: %s' % (
        pack_id, ORDER_SUFFIX, ', '.join(lines) or 'no dependency'))


def _lookup_mod():
    try:
        spec = importlib.util.spec_from_file_location('i18n_lookup', LOOKUP_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if hasattr(mod, 'order_layers') else None
    except Exception:
        return None


def check(packs_dir):
    mod = _lookup_mod()
    if mod is None:
        print('[kit-i18n-layer] WARN: layer order check skipped '
              '(lookup not found at %s)' % LOOKUP_PY)
        return
    by_lang = {}
    for p in glob.glob(os.path.join(glob.escape(packs_dir), '*.i18n.*.json')):
        if not os.path.isfile(p):
            continue
        stem = os.path.basename(p)[:-len('.json')]
        pid, _, lang = stem.rpartition('.i18n.')
        by_lang.setdefault(lang, []).append(pid)
    ids = sorted({i for v in by_lang.values() for i in v},
                 key=lambda i: i.encode('utf-8'))
    unrecorded = [i for i in ids if not mod.read_order(packs_dir, i)[2]]
    if unrecorded and len(ids) > 1:
        print('[kit-i18n-layer] WARN: no layer order record for %s -- name '
              'order decides for them until the next install/update derives '
              'it' % ', '.join(unrecorded))
    for lang in sorted(by_lang):
        lids = sorted(by_lang[lang],
                      key=lambda i: ('%s.i18n.%s.json' % (i, lang)).encode('utf-8'))
        ordered, cycle = mod.order_layers(packs_dir, lids)
        if cycle:
            print('[kit-i18n-layer] WARN: layer dependency cycle (%s) -- '
                  'order: %s (name order for the cycle)'
                  % (lang, ' > '.join(ordered)))


def _auto_check(packs_dir):
    if os.environ.get('DOGANY_KIT_I18N_DEFER_CHECK') != '1':
        check(packs_dir)


def main(argv):
    if len(argv) in (4, 5) and argv[0] == 'sync':
        rc = sync(argv[1], argv[2], argv[3])
        if rc == 0 and len(argv) == 5:
            write_order(argv[1], argv[2], argv[4])
            _auto_check(argv[1])
        return rc
    if len(argv) == 4 and argv[0] == 'order':
        write_order(argv[1], argv[2], argv[3])
        _auto_check(argv[1])
        return 0
    if len(argv) == 2 and argv[0] == 'check':
        check(argv[1])
        return 0
    print('usage: kit_i18n_layer.py sync <packs_dir> <pack_id> <bundle_dir> '
          '[<manifest>] | order <packs_dir> <pack_id> <manifest> | '
          'check <packs_dir>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
