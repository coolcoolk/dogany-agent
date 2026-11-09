#!/usr/bin/env python3
"""DGN-2256: receipt/hash ownership and journaled, local-only layout recovery.

Called with the lease holder locked by crew_lease.sh. Recovery never installs a
pack or controls a service. All members are preflighted before the first write.
The journal retains exact preimages; undo refuses intervening edits.
"""
import base64
import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile


def need(ok, message):
    if not ok:
        raise ValueError(message)


def relative(value):
    need(isinstance(value, str) and value and not value.startswith('/')
         and all(p not in ('', '.', '..') for p in value.split('/')), 'unsafe path: ' + str(value))
    return value


def safe_path(root, name):
    path = root / relative(name)
    for parent in [path.parent, *path.parent.parents]:
        if parent == root:
            break
        need(not parent.is_symlink(), 'directory alias refused: ' + str(parent))
    need(not root.is_symlink(), 'root alias refused: ' + str(root))
    return path


def candidate(root, txn):
    need(re.fullmatch(r'pack/[a-z][a-z0-9_-]*/v[^/]+', txn), 'invalid transaction')
    path = safe_path(root, 'database/.gen/' + txn + '/candidate.json')
    need(not path.is_symlink(), 'candidate alias refused')
    doc = json.loads(path.read_text())
    names = doc.get('entries')
    need(doc.get('txn') == txn and doc.get('pack_id') == txn.split('/')[1], 'candidate identity mismatch')
    need(isinstance(names, list) and names and all(isinstance(n, str) for n in names)
         and len(set(names)) == len(names), 'invalid candidate entries')
    for name in names:
        relative(name)
    return doc


def ledger(root, pack):
    path = safe_path(root, 'config/packs/' + pack + '.files')
    need(not path.is_symlink(), 'ledger alias refused')
    lines = path.read_text().splitlines()
    need('# pack: ' + pack in lines, 'missing installed-file ownership header')
    return set(n for n in lines if n and not n.startswith('#'))


def owned(root, txn, rel, name):
    doc = candidate(root, txn)
    need(name in doc['entries'], 'entry absent from candidate')
    need(rel in ('database', 'database/kits/' + doc['pack_id']), 'invalid consumer root')
    live = safe_path(root, rel + '/' + relative(name))
    gen = safe_path(root, 'database/.gen/' + txn + '/' + name)
    need(rel + '/' + name in ledger(root, doc['pack_id']), 'file absent from installed-file receipt')
    need(live.is_file() and not live.is_symlink() and gen.is_file() and not gen.is_symlink(),
         'real installed/staged file required')
    need(live.read_bytes() == gen.read_bytes(), 'installed/staged hash mismatch: ' + str(live))


def image(path):
    if path.is_symlink():
        return dict(kind='link', target=os.readlink(path))
    if not path.exists():
        return dict(kind='absent')
    need(path.is_file(), 'non-file refused: ' + str(path))
    return dict(kind='file', data=base64.b64encode(path.read_bytes()).decode(),
                mode=stat.S_IMODE(path.stat().st_mode))


def file_image(data, mode):
    return dict(kind='file', data=base64.b64encode(data).decode(), mode=mode)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if value['kind'] == 'absent':
        if path.exists() or path.is_symlink():
            path.unlink()
        return
    fd, temporary = tempfile.mkstemp(prefix='.layout-recovery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            if value['kind'] == 'file':
                out.write(base64.b64decode(value['data']))
                out.flush()
                os.fchmod(out.fileno(), value['mode'])
                os.fsync(out.fileno())
        if value['kind'] == 'link':
            os.unlink(temporary)
            os.symlink(value['target'], temporary)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def payload(repo, pack, kit, txn):
    # Read only the retained local tag, never a working tree or network source.
    archive = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar',
                              'refs/tags/' + txn, 'pack-manifest.json', 'payload/database'],
                             check=True, capture_output=True).stdout
    files = {}
    manifest = None
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isdir():
                continue
            need(member.isfile(), 'non-file in retained payload')
            data = tar.extractfile(member).read()
            if member.name == 'pack-manifest.json':
                manifest = json.loads(data)
            elif member.name.startswith('payload/database/'):
                name = relative(member.name[len('payload/database/'):])
                if name != '.gitkeep':
                    files[name] = (data, member.mode)
    need(manifest and manifest.get('id') == pack and manifest.get('provides_kit') == kit
         and manifest.get('pack_version') == txn.rsplit('/v', 1)[1], 'retained manifest identity mismatch')
    need(files, 'empty retained DB payload')
    return files


def recovery_plan(members, pack, kit, txn, files):
    preserved, generations, consumers = [], [], []
    for root in members:
        doc = candidate(root, txn)
        need(set(doc['entries']) == set(files), 'candidate/retained payload entry mismatch')
        db = root / 'database'
        need((db / '.gen-active').is_symlink() and os.readlink(db / '.gen-active') == '.gen/' + txn,
             'repair requires the interrupted generation active')
        aliases = [db / (kit + '.' + ext) for ext in ('py', 'sh')]
        scoped = all(p.is_symlink() and os.readlink(p) == 'kits/' + kit + '/' + p.name for p in aliases)
        rel = 'database/kits/' + kit if scoped else 'database'
        need(doc.get('database_root', rel) == rel, 'candidate consumer root mismatch')
        installed = ledger(root, pack)
        for name, (data, mode) in files.items():
            live = safe_path(root, rel + '/' + name)
            gen = safe_path(root, 'database/.gen/' + txn + '/' + name)
            target = '../' * (len(PurePosixPath(name).parts) - 1) + '.gen-active/' + name
            if scoped:
                target = '../../' + target
            need(rel + '/' + name in installed, 'kit consumer absent from installed-file receipt: ' + str(live))
            if live.is_symlink():
                need(os.readlink(live) == target, 'foreign kit consumer alias: ' + str(live))
            need(live.is_file() and live.read_bytes() == data,
                 'kit consumer differs from retained payload: ' + str(live))
            # Preserve flat consumers captured by the historical migrator before
            # repairing generation bytes. This explicitly includes lifekit schema.
            flat = safe_path(root, 'database/' + name)
            flat_target = '../' * (len(PurePosixPath(name).parts) - 1) + '.gen-active/' + name
            captured = scoped and flat.is_symlink() and os.readlink(flat) == flat_target
            if captured:
                need(flat.is_file(), 'dangling captured flat consumer')
                after = file_image(flat.read_bytes(), stat.S_IMODE(flat.stat().st_mode))
                preserved.append(dict(root=str(root), path='database/' + name, before=image(flat), after=after))
            need(gen.is_file() and not gen.is_symlink(), 'missing/alias generation file')
            if gen.read_bytes() != data:
                need(captured, 'unexplained generation mismatch: ' + str(gen))
                generations.append(dict(root=str(root), path='database/.gen/' + txn + '/' + name,
                                       before=image(gen), after=file_image(data, mode)))
            if not live.is_symlink():
                consumers.append(dict(root=str(root), path=rel + '/' + name, before=image(live),
                                       after=dict(kind='link', target=target)))
    # Detach every foreign consumer before changing generation bytes. Only
    # expose the scoped CLIs after their whole generation is correct.
    return preserved + generations + consumers


def fingerprint(value):
    if value['kind'] != 'file':
        return value
    return dict(kind='file', mode=value['mode'],
                sha256=hashlib.sha256(base64.b64decode(value['data'])).hexdigest())


def retained_identity(files):
    return {name: dict(sha256=hashlib.sha256(data).hexdigest(), mode=mode)
            for name, (data, mode) in files.items()}


def recovery_proof(lease, members, pack, kit, txn, repo, files, operations):
    # Include unchanged members and inputs which decide ownership, candidate
    # identity and alias resolution. Mutation pre/postimages are the only
    # permitted drift; unchanged inputs must remain byte-for-byte identical.
    paths = set()
    for root in members:
        doc = candidate(root, txn)
        scoped = all((root / ('database/' + kit + '.' + ext)).is_symlink() and
                     os.readlink(root / ('database/' + kit + '.' + ext)) ==
                     'kits/' + kit + '/' + kit + '.' + ext for ext in ('py', 'sh'))
        rel = 'database/kits/' + kit if scoped else 'database'
        names = ['.instance.conf', 'config/packs/' + pack + '.files',
                 'config/packs/' + pack + '.manifest.json',
                 'config/packs/' + pack + '.receipt',
                 'config/packs/' + pack + '.receipt.inprogress',
                 'database/.gen-active', 'database/.gen/' + txn + '/candidate.json',
                 'database/' + kit + '.py', 'database/' + kit + '.sh']
        for name in doc['entries']:
            names.extend([rel + '/' + name, 'database/' + name,
                          'database/.gen/' + txn + '/' + name])
        for name in names:
            paths.add(safe_path(root, name))
    paths.update(lease / name for name in ('holder', 'members', 'state'))
    paths.add(lease.parent / 'crew.conf')
    paths.add(Path(os.environ.get('DOGANY_PACK_SOURCES',
                                 str(lease.parents[2] / 'pack-sources.conf'))))
    changed = {str(Path(row['root']) / row['path']): row for row in operations}
    proof = []
    for path in sorted(paths):
        path = path.parent.resolve() / path.name
        row = changed.get(str(path))
        allowed = [fingerprint(row[key]) for key in ('before', 'after')] if row else [fingerprint(image(path))]
        proof.append(dict(path=str(path), allowed=allowed))
    return dict(inputs=proof, repo=str(repo), pack=pack, kit=kit,
                payload=retained_identity(files))


def verify_proof(doc):
    proof = doc['proof']
    for row in proof['inputs']:
        path = Path(row['path'])
        # Recheck directory aliases as well as the leaf's own image.
        for parent in path.parents:
            need(not parent.is_symlink(), 'proof directory alias refused: ' + str(parent))
        need(fingerprint(image(path)) in row['allowed'], 'recovery proof drift: ' + str(path))
    files = payload(Path(proof['repo']), proof['pack'], proof['kit'], doc['txn'])
    need(retained_identity(files) == proof['payload'], 'retained payload identity drift')


def verify_clear(lease, members, txn):
    journal = lease / 'layout-recovery.json'
    if journal.exists():
        doc = json.loads(journal.read_text())
        validate_journal(doc, members, txn)
        verify_proof(doc)


def validate_journal(doc, members, txn):
    need(doc.get('schema') == 2 and doc.get('txn') == txn
         and doc.get('members') == [str(p) for p in members], 'journal identity mismatch')
    for row in doc['operations']:
        need(row['root'] in doc['members'] and relative(row['path']).startswith('database/'),
             'journal operation outside member database')


def apply_journal(doc, undoing=False):
    rows = list(reversed(doc['operations'])) if undoing else doc['operations']
    # Refuse edits across the entire crew before restoring or resuming anything.
    for row in rows:
        path = safe_path(Path(row['root']), row['path'])
        need(image(path) in (row['before'], row['after']), 'intervening edit; journal refused: ' + str(path))
    for row in rows:
        path = safe_path(Path(row['root']), row['path'])
        want = row['before' if undoing else 'after']
        if image(path) != want:
            put(path, want)
        need(image(path) == want, 'recovery postcondition failed: ' + str(path))


def repair(lease, members, pack, kit, txn, repo):
    journal = lease / 'layout-recovery.json'
    if journal.exists():
        doc = json.loads(journal.read_text())
        validate_journal(doc, members, txn)
        verify_proof(doc)
    else:
        files = payload(repo, pack, kit, txn)
        operations = recovery_plan(members, pack, kit, txn, files)
        doc = dict(schema=2, txn=txn, members=[str(p) for p in members], operations=operations,
                   proof=recovery_proof(lease, members, pack, kit, txn, repo, files, operations))
        verify_proof(doc)
        put(journal, file_image((json.dumps(doc, indent=2) + '\n').encode(), 0o600))
    apply_journal(doc)
    print('[crew-layout] repaired from retained tag; reversible preimages: ' + str(journal))


def undo(lease, members, txn):
    doc = json.loads((lease / 'layout-recovery.json').read_text())
    validate_journal(doc, members, txn)
    verify_proof(doc)
    apply_journal(doc, undoing=True)


if __name__ == '__main__':
    try:
        need(len(sys.argv) == 6 and sys.argv[1] == 'owned', 'usage: crew_layout_recovery.py owned ROOT TXN REL NAME')
        owned(Path(sys.argv[2]), *sys.argv[3:])
    except (OSError, ValueError, KeyError) as error:
        print('[crew-layout] REFUSE: ' + str(error), file=sys.stderr)
        sys.exit(2)
