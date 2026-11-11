#!/usr/bin/env python3
"""Read-only ownership gate for every kit database delivery destination."""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from crew_layout_recovery import candidate, ledger, need, relative, safe_path


def preflight(root, pack, rel, names):
    need(rel == 'database' or rel.startswith('database/kits/'), 'invalid database root')
    installed = None
    for name in names:
        relative(name)
        live = safe_path(root, rel + '/' + name)
        if not live.exists() and not live.is_symlink():
            continue
        legacy_primary = name == pack + '.py' and live.is_file() and not live.is_symlink()
        if installed is None and not legacy_primary:
            installed = ledger(root, pack)
        # A pre-contract installation can have only the conventional primary
        # module and no files ledger.  DGN-2291 captures that file as unknown
        # local intent and restores it after delivery; other unreceipted names
        # (especially shared schema/aliases) remain a hard ownership conflict.
        need(legacy_primary or rel + '/' + name in installed,
             'database destination not owned by pack: ' + str(live))
        if live.is_symlink():
            # Only the exact generation consumer is ours. In particular a
            # scoped alias to ../../schema.sql must never reach publication.
            target = '../' * (len(Path(name).parts) - 1) + '.gen-active/' + name
            if rel != 'database':
                target = '../../' + target
            need(os.readlink(live) == target, 'foreign database alias: ' + str(live))
            active = safe_path(root, 'database/.gen-active')
            need(active.is_symlink(), 'missing active database generation')
            active_target = os.readlink(active)
            need(active_target.startswith('.gen/'), 'foreign active generation')
            txn = active_target[len('.gen/'):]
            doc = candidate(root, txn)
            need(doc['pack_id'] == pack and name in doc['entries'], 'foreign active candidate')
            need(doc.get('database_root', rel) == rel, 'active consumer root mismatch')
            gen = safe_path(root, 'database/.gen/' + txn + '/' + name)
            need(gen.is_file() and not gen.is_symlink(), 'missing/alias database generation file')
        else:
            need(live.is_file(), 'non-file database destination: ' + str(live))


if __name__ == '__main__':
    try:
        root, pack, rel, payload, *selected = sys.argv[1:]
        names = selected or [str(p.relative_to(payload)) for p in Path(payload).rglob('*')
                             if p.is_file() and p.name != '.gitkeep']
        preflight(Path(root), pack, rel, names)
    except (OSError, ValueError, KeyError) as error:
        print('[database-delivery] REFUSE: ' + str(error), file=sys.stderr)
        sys.exit(2)
