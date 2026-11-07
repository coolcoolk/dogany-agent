#!/usr/bin/env python3
"""WP8 pack hand-off only. View composition and migration belong to WP9/WP10."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HEALTH = {'exercise', 'nutrition', 'sleep-recovery'}
FRAME = {'README.md', 'REFRACTION.md', 'registry.yaml', 'axes.yaml', 'lanes.yaml'}
TOKEN = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')


def item_id(path):
    lines = path.read_text(encoding='utf-8').splitlines()
    if not lines or lines[0] != '---':
        return ''
    ids = []
    for line in lines[1:]:
        if line == '---':
            return ids[0] if len(ids) == 1 else ''
        if line.startswith('id:'):
            ids.append(line[3:].strip().strip('\"\''))
    return ''


def plan(manifest, payload):
    """Only frame and declared health bootstrap. Never read instance custom data."""
    if 'knowledge_modules' not in manifest or manifest['kind'] == 'kit':
        return []
    wh = payload / 'knowledge' / manifest['knowledge_warehouse']
    rows = []
    for source in sorted(wh.rglob('*')):
        if source.is_symlink():
            raise ValueError('knowledge payload symlink refused: ' + str(source))
        if not source.is_file():
            continue
        rel = source.relative_to(wh)
        if (len(rel.parts) == 1 and rel.name in FRAME) or rel.parts[0] == 'tools':
            rows.append((source, rel))
        elif rel.parts[0] == 'items' and source.suffix == '.md':
            ident = item_id(source)
            module, sep, tail = ident.partition('/')
            if not sep or not TOKEN.fullmatch(module) or not re.fullmatch(r'[a-zA-Z0-9_-]+', tail):
                print('knowledge: skipped invalid item id: ' + str(rel), file=sys.stderr)
                continue
            if module not in HEALTH or module not in manifest['knowledge_modules']:
                raise ValueError('knowledge bootstrap requires declared health module: ' + ident)
            if len(rel.parts) != 2 or source.name != module + '-' + tail + '.md':
                print('knowledge: skipped source filename mismatch: ' + str(rel), file=sys.stderr)
                continue
            rows.append((source, Path('.pack-items') / source.name))
    return rows


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record(root, manifest):
    """Per-owner declarations allow reinstallation without losing kit contributions."""
    owner = manifest['id']
    if not TOKEN.fullmatch(owner):
        raise ValueError('invalid declaration owner')
    conf = root / '.instance.conf'
    lines = conf.read_text().splitlines() if conf.exists() else []
    installed = set()
    for line in lines:
        if line.startswith('DOGANY_PACKS='):
            installed.update(p.split('@')[0] for p in line.split('=', 1)[1].split(','))
    installed.add(owner)
    target = root / 'config' / 'packs' / (owner + '.knowledge.json')
    if 'knowledge_modules' in manifest:
        value = {k: manifest[k] for k in ('id', 'kind', 'knowledge_warehouse', 'knowledge_modules')}
        atomic_text(target, json.dumps(value, sort_keys=True) + '\n')
    elif target.exists():
        target.unlink()
    else:
        return
    warehouses = {}
    for path in sorted(target.parent.glob('*.knowledge.json')):
        row = json.loads(path.read_text())
        if row['id'] in installed:
            warehouses.setdefault(row['knowledge_warehouse'], set()).update(row['knowledge_modules'])
    declaration = ';'.join(wh + ':' + ','.join(sorted(modules)) for wh, modules in sorted(warehouses.items()))
    lines = [line for line in lines if not line.startswith('DOGANY_KNOWLEDGE=')]
    if declaration:
        lines.append('DOGANY_KNOWLEDGE=' + declaration)
    atomic_text(conf, '\n'.join(lines) + '\n')


def build(root, warehouse):
    """Explicit future seam: no synthetic frame, no bootstrap masquerading as a view."""
    frame = root / 'knowledge' / warehouse
    if not all((frame / name).is_file() for name in FRAME):
        print('PENDING_OWNER_CONFIRM_KNOWLEDGE_NOT_READY')
        return 3
    builder = Path(__file__).with_name('knowledge_view.py')
    if not builder.is_file():
        print('PENDING_OWNER_CONFIRM_KNOWLEDGE_BUILD_UNAVAILABLE')
        return 3
    # Existing flat items must never pass first-build hand-edit capture until
    # WP10 has archived and classified their provenance.
    if (frame / 'items').exists() and not (frame / '.view-manifest').exists():
        print('PENDING_OWNER_CONFIRM_KNOWLEDGE_MIGRATION_REQUIRED')
        return 3
    return subprocess.run([sys.executable, str(builder), '--instance', str(root),
                           '--warehouse', warehouse], check=False).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=('check', 'stage', 'record', 'build'))
    ap.add_argument('--manifest', type=Path)
    ap.add_argument('--payload', type=Path)
    ap.add_argument('--destination', type=Path)
    ap.add_argument('--instance', type=Path)
    ap.add_argument('--warehouse')
    args = ap.parse_args()
    if args.action == 'build':
        if not args.warehouse or not TOKEN.fullmatch(args.warehouse):
            raise ValueError('invalid warehouse')
        return build(args.instance, args.warehouse)
    manifest = json.loads(args.manifest.read_text())
    if args.action == 'record':
        record(args.instance, manifest)
    else:
        rows = plan(manifest, args.payload)
        if args.action == 'stage':
            for source, rel in rows:
                dest = args.destination / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
            (args.destination / '.pack-items').mkdir(parents=True, exist_ok=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as exc:
        print('knowledge hand-off: ' + str(exc), file=sys.stderr)
        sys.exit(1)
