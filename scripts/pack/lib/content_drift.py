#!/usr/bin/env python3
"""content_drift.py -- content-drift scanner, SINGLE SOURCE (DGN-1109 / DGN-1118).

One judgement, two callers (DGN-1034 shape guard: never fork this logic):
  * scripts/pack/pack_install.sh  -- reinstall/upgrade lane (DGN-1109; dir
    specs + the per-spec BASELINE field when the instance's DOGANY_PACKS pin
    resolves to a pack tag, DGN-1491; no substitution/filter flags -- with
    no baseline attached the behavior stays byte-for-byte the original
    embedded heredoc this file was extracted from).
  * update.sh                     -- framework-update lane (DGN-1118; adds
    baseline / substitution / preserve / only / exclude, all opt-in flags
    that leave the strict lane semantics untouched when absent).

Signal, per file that exists on both sides with differing content
(DGN-1109 design, do not reinvent):
    lost = lines present in live but absent from the payload (set
           difference -- a pure reorder/reformat counts 0; blank lines
           ignored). lost==0 (payload superset / pure addition) => safe.
    DESTRUCTIVE when lost > 0 AND any shrink signature holds:
      - top-level symbol loss (py ^def/^class, sh name())
      - net line shrink >= 5 (live lines - payload lines)
      - byte halving (live >= 1024B and payload < half of live)
    DRIFT (loud enumerate, caller proceeds) when lost > 0 without any
    shrink signature. Hash mismatch ALONE never judges (false-positive
    source).

Baseline (DGN-1118): when a spec carries a 4th field, it names the VENDOR
tree of the content the live side is expected to have started from (the
instance's currently-landed framework tag). Two effects:
  - live == baseline  -> PASS outright: the live copy is untouched vendor
    content, so any payload change (including a legitimate vendor SHRINK /
    refactor) is vendor progression, not user loss. This is what makes a
    normal update zero-false-positive even when the vendor removes lines.
  - lost / symbol-loss are counted NET OF BASELINE: a line/symbol that the
    baseline also carries was vendor-authored, its removal is the vendor's
    decision, not user loss.
No baseline field -> strict DGN-1109 signal (both callers degrade to this
when no baseline resolves; a pack-lane FRESH install has no baseline by
definition, DGN-1491).

DGN-2252: archived payload/baseline symlinks compare their resolved content.
For a Python compatibility shim, payload comparison includes the imported
owner modules copied by the same spec. Exported owner symbols are preserved;
text differences still use the ordinary loss/shrink signals. Reported payload
line/byte counts include that owner content, not only the short shim.

CLI:
  content_drift.py [flags] SPEC...
    SPEC = 'SURFACE<TAB>PAYLOAD<TAB>LIVE[<TAB>BASELINE]'
           roots are dirs (walked) or single files (compared 1:1; the
           surface label is then the instance-relative path of the file).
  --subst TOKEN=VALUE   literal replacement applied to PAYLOAD and BASELINE
                        text before any comparison (mint placeholder
                        parity: the live side is post-substitution).
                        Repeatable; applied in argument order.
  --preserve ENTRY      instance-root-relative preserve entry (exact file,
                        or 'dir/' prefix). A file whose SURFACE/REL matches
                        is skipped: it will not be copied, so it must not
                        be judged (lockstep with .dogany-preserve).
  --only 'SURF:PAT'     when >=1 --only exists for a surface, only files
                        matching one of them are scanned (rsync include
                        parity, e.g. memory-engine '*.py').
  --exclude 'SURF:PAT'  skip matching files. SURF '*' = every surface.
                        PAT: leading '/' anchors to the transfer root;
                        a '/' inside matches the relpath; otherwise it
                        matches the basename or any path component.
  --kit-layer 'SURFACE_REL=LAYER'
                        DGN-1789: LAYER is an installed pack's i18n override
                        layer (flat JSON object) for the live i18n file named
                        by SURFACE_REL (e.g. config/i18n/ko.json). A live line
                        '"key": value' whose key AND value the layer carries
                        is dropped from the live side before judging: the
                        shared lookup keeps serving it from the layer after
                        the payload overwrites the file (old add-only merge
                        residue, not user content); in that file a member
                        line's trailing ',' is not judged either (the merge
                        added one to the previous last member). Repeatable;
                        no other file is affected.

Exit: 0 = PASS, 3 = DRIFT only, 4 = any DESTRUCTIVE, 2 = usage/scan error
(callers treat unknown codes as scanner failure and fail closed, DGN-1004).
Output rows and the SUMMARY line are consumed verbatim by both callers.
"""

import ast
import fnmatch
import json
import os
import re
import sys

PY_SYM = re.compile(r'^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)')
SH_SYM = re.compile(r'^(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)')
# One '"key": value' member line of a flat JSON object (indent-agnostic,
# optional trailing comma).
JSON_MEMBER = re.compile(r'^\s*("(?:[^"\\]|\\.)*")\s*:\s*(.*?)\s*,?\s*$')


def load_layer_pairs(path):
    """(key, value) pairs a kit i18n layer serves; raises on a bad layer."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('%s is not a JSON object' % path)
    return {(k, json.dumps(v, sort_keys=True)) for k, v in data.items()}


def layer_served(line, pairs):
    m = JSON_MEMBER.match(line)
    if not m:
        return False
    try:
        key = json.loads(m.group(1))
        val = json.loads(m.group(2))
    except ValueError:
        return False
    return (key, json.dumps(val, sort_keys=True)) in pairs


def symbols(rel, lines):
    ext = os.path.splitext(rel)[1]
    pat = PY_SYM if ext == '.py' else (SH_SYM if ext == '.sh' else None)
    syms = set()
    if pat is None:
        return syms
    for ln in lines:
        m = pat.match(ln)
        if m:
            syms.add(m.group(1))
    return syms


def shim_owners(src, text):
    """Read sibling owner modules of a Python compatibility shim, never import.

    Only import/docstring/main-entry wrappers qualify. An ordinary module
    importing a helper must not gain credit for all of that helper's symbols.
    Return paths plus imported symbol names (None for a star import).
    """
    if not src.endswith('.py'):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    owners = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if not node.module or node.level > 1:
                return []
            path = os.path.join(os.path.dirname(src),
                                *node.module.split('.')) + '.py'
            names = None if any(n.name == '*' for n in node.names) else {
                n.name for n in node.names if (n.asname or n.name) == n.name}
            owners.append((path, names))
        elif isinstance(node, ast.Import):
            continue
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        elif isinstance(node, ast.If) and ast.dump(node.test) == ast.dump(
                ast.parse('__name__ == "__main__"', mode='eval').body):
            continue
        else:
            return []
    return owners


def tree_bytes(path):
    """git archive materializes mode-120000 blobs as links to canonical code."""
    with open(os.path.realpath(path), 'rb') as f:
        return f.read()


def excluded_builtin(surface, rel, base):
    # Mirrors the pack copy-step excludes 1:1 (mirror surface only; .gitkeep
    # all) -- unchanged from the DGN-1109 heredoc this file was extracted from.
    if base == '.gitkeep':
        return True
    if surface == 'mirror':
        for suf in ('.db', '.db-wal', '.db-shm', '.db-journal', '.pyc'):
            if base.endswith(suf):
                return True
        if '.db.bak' in base or base == 'download.html':
            return True
        if '__pycache__' in rel.split(os.sep):
            return True
    return False


def pat_matches(pat, rel, base):
    if pat.startswith('/'):
        return fnmatch.fnmatch(rel, pat[1:])
    if '/' in pat:
        return fnmatch.fnmatch(rel, pat)
    if fnmatch.fnmatch(base, pat):
        return True
    return any(fnmatch.fnmatch(part, pat) for part in rel.split(os.sep)[:-1])


def flag_excluded(surface, rel, base, only_pats, excl_pats):
    for surf, pat in excl_pats:
        if surf in ('*', surface) and pat_matches(pat, rel, base):
            return True
    onlys = [p for s, p in only_pats if s in ('*', surface)]
    if onlys and not any(pat_matches(p, rel, base) for p in onlys):
        return True
    return False


def preserved(surface_rel, preserve_entries):
    # is_preserved() parity (update.sh): exact match, or trailing-'/' prefix.
    for e in preserve_entries:
        if e == surface_rel:
            return True
        if e.endswith('/') and surface_rel.startswith(e):
            return True
    return False


def main(argv):
    substs, preserve_entries, only_pats, excl_pats, specs = [], [], [], [], []
    kit_layers = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--subst', '--preserve', '--only', '--exclude',
                 '--kit-layer'):
            if i + 1 >= len(argv):
                print('content_drift: missing value for %s' % a, file=sys.stderr)
                return 2
            v = argv[i + 1]
            i += 2
            if a == '--subst':
                if '=' not in v:
                    print('content_drift: bad --subst %r' % v, file=sys.stderr)
                    return 2
                substs.append(tuple(v.split('=', 1)))
            elif a == '--preserve':
                preserve_entries.append(v)
            elif a == '--kit-layer':
                if '=' not in v:
                    print('content_drift: bad --kit-layer %r' % v,
                          file=sys.stderr)
                    return 2
                rel, layer = v.split('=', 1)
                try:
                    kit_layers.setdefault(rel, set()).update(
                        load_layer_pairs(layer))
                except (OSError, ValueError) as e:
                    print('content_drift: unreadable --kit-layer %r (%s)'
                          % (v, e), file=sys.stderr)
                    return 2
            else:
                if ':' not in v:
                    print('content_drift: bad %s %r' % (a, v), file=sys.stderr)
                    return 2
                pair = tuple(v.split(':', 1))
                (only_pats if a == '--only' else excl_pats).append(pair)
        else:
            specs.append(a)
            i += 1

    def subst(text):
        for tok, val in substs:
            text = text.replace(tok, val)
        return text

    destructive, drift, total_lost = [], [], 0
    for spec in specs:
        parts = spec.split('\t')
        if len(parts) == 3:
            surface, proot, lroot = parts
            broot = ''
        elif len(parts) == 4:
            surface, proot, lroot, broot = parts
        else:
            print('content_drift: bad spec %r' % spec, file=sys.stderr)
            return 2

        if os.path.isfile(proot):
            # Single-file spec: surface labels the instance-relative path.
            items = [(os.path.basename(proot), proot, lroot,
                      broot if broot else None, surface, surface)]
        elif os.path.isdir(proot):
            items = []
            for dirpath, dirnames, filenames in os.walk(proot):
                dirnames.sort()
                for fn in sorted(filenames):
                    src = os.path.join(dirpath, fn)
                    rel = os.path.relpath(src, proot)
                    items.append((rel, src, os.path.join(lroot, rel),
                                  os.path.join(broot, rel) if broot else None,
                                  '%s/%s' % (surface, rel), surface))
        else:
            continue

        for rel, src, dst, basef, surface_rel, surf in items:
            base = os.path.basename(rel)
            if excluded_builtin(surf, rel, base):
                continue
            if flag_excluded(surf, rel, base, only_pats, excl_pats):
                continue
            if preserved(surface_rel, preserve_entries):
                continue
            if os.path.islink(dst) or not os.path.isfile(dst):
                continue
            pb = tree_bytes(src)
            with open(dst, 'rb') as f:
                lb = f.read()
            if pb == lb:
                continue
            ltext = lb.decode('utf-8', 'replace')
            ptext = subst(pb.decode('utf-8', 'replace'))
            if substs:
                if ptext == ltext:
                    continue
                pb = ptext.encode('utf-8')
            blines = set()
            bsyms = set()
            if basef is not None and (os.path.isfile(basef) or os.path.islink(basef)):
                btext = subst(tree_bytes(basef).decode('utf-8', 'replace'))
                if btext == ltext:
                    continue  # untouched vendor content: vendor progression
                blines = set(btext.splitlines())
                bsyms = symbols(rel, btext.splitlines())
            plines = ptext.splitlines()
            llines = ltext.splitlines()
            psyms = symbols(rel, plines)
            # DGN-2252: the deployed entry point can shrink to a legacy shim
            # while its code ships in the same transfer under an owner name.
            # Credit only owners actually copied by this directory spec;
            # a missing/excluded/preserved owner is not preservation evidence.
            for owner, imported in shim_owners(src, ptext):
                if not os.path.isdir(proot) or not os.path.isfile(owner):
                    continue
                orel = os.path.relpath(owner, proot)
                if (excluded_builtin(surf, orel, os.path.basename(owner)) or
                        flag_excluded(surf, orel, os.path.basename(owner), only_pats, excl_pats) or
                        preserved('%s/%s' % (surface, orel), preserve_entries)):
                    continue
                otext = subst(tree_bytes(owner).decode('utf-8', 'replace'))
                otree = ast.parse(otext)
                osyms = symbols(orel, otext.splitlines())
                # An explicit export list can hide definitions from import *.
                if imported is None:
                    imported = {s for s in osyms if not s.startswith('_')}
                    for node in otree.body:
                        if isinstance(node, ast.Assign) and any(
                                isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets):
                            try:
                                imported = set(ast.literal_eval(node.value))
                            except (ValueError, TypeError):
                                imported = set()
                psyms.update(osyms & imported)
                olines = otext.splitlines()
                hidden = set()
                for node in otree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                            and node.name not in imported:
                        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                        hidden.update(range(first - 1, node.end_lineno))
                olines = [line for n, line in enumerate(olines) if n not in hidden]
                plines.extend(olines)
                pb += '\n'.join(olines).encode('utf-8')
            served = kit_layers.get(surface_rel)
            if served:
                # The add-only merge appended its keys after the file's last
                # member, which also gained a ',' -- a merge artifact, so a
                # member line's trailing comma is not judged in this file.
                llines = [l.rstrip().rstrip(',') for l in llines
                          if not layer_served(l, served)]
                plines = [l.rstrip().rstrip(',') for l in plines]
                blines = {l.rstrip().rstrip(',') for l in blines}
                lb = '\n'.join(llines).encode('utf-8')
            lost_set = (set(llines) - set(plines)) - blines
            lost_set.discard('')
            lost = len([l for l in lost_set if l.strip()])
            if lost == 0:
                continue
            net_shrink = len(llines) - len(plines)
            byte_halved = len(lb) >= 1024 and len(pb) * 2 < len(lb)
            syms_lost = sorted(
                (symbols(rel, llines) - psyms) - bsyms)
            row = (surface_rel, len(llines), len(plines), len(lb), len(pb),
                   lost, syms_lost)
            total_lost += lost
            if syms_lost or net_shrink >= 5 or byte_halved:
                destructive.append(row)
            else:
                drift.append(row)

    for kind, rows in (('DESTRUCTIVE', destructive), ('DRIFT', drift)):
        for surface_rel, ll, pl, lbn, pbn, lost, syms in rows:
            extra = ''
            if syms:
                shown = ', '.join(syms[:8])
                if len(syms) > 8:
                    shown += ', ... +%d more' % (len(syms) - 8)
                extra = ' -- symbols lost: %s' % shown
            print('%s %s: live %d lines/%dB -> payload %d lines/%dB '
                  '(%d live-only lines lost)%s'
                  % (kind, surface_rel, ll, lbn, pl, pbn, lost, extra))
    print('SUMMARY destructive=%d drift=%d live_only_lines=%d'
          % (len(destructive), len(drift), total_lost))
    return 4 if destructive else (3 if drift else 0)



def override_reconcile(argv):
    """DGN-1766: plan without writes; apply only after update preflights.

    Preserve still excludes the file from every ordinary copy step. This
    path rebases its local delta using git merge-file, never conflict markers
    in live code. Per-file bases survive a held update and version advances.
    Reports are local review items for framework-maintainer; no notifications are sent.
    """
    import argparse
    import base64
    import hashlib
    from pathlib import Path
    import stat
    import subprocess
    import tempfile

    def encode(data):
        return base64.b64encode(data).decode('ascii')

    def decode(data):
        return base64.b64decode(data) if data is not None else None

    def safe(path, root):
        # Reject symlinks in ANY component, including the state directory.
        path = Path(os.path.abspath(path))
        root = Path(os.path.abspath(root))
        if path != root and root not in path.parents:
            return False
        return not any(p.is_symlink() for p in (path, *path.parents)
                       if p == root or root in p.parents)

    def atomic(path, data, mode=0o600):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.override-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                os.fchmod(out.fileno(), mode)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def current(path):
        # An unreadable pin is held for review, never fatal (DGN-827 P4).
        try:
            return path.read_bytes()
        except OSError:
            return None

    if argv[0] == '--override-apply':
        plan = json.load(sys.stdin)
        root = Path(plan['root'])
        state = root / '.claude/override-reconcile'
        if not safe(state, root):
            raise ValueError('unsafe override state directory')
        report = []
        for row in plan['files']:
            key = row['path']
            dst = root / key
            ident = hashlib.sha256(key.encode()).hexdigest()
            record = state / (ident + '.json')
            if not safe(record, root) or not safe(dst, root):
                row['status'] = 'REVIEW'
                row['reason'] = 'symlink or unsafe path; copy held'
            elif (not dst.is_file() or
                  current(dst) != decode(row['local']) or
                  (current(record) if record.exists() else None)
                  != decode(row['record'])):
                row['status'] = 'REVIEW'
                row['reason'] = 'file or baseline changed after planning; copy held'
            else:
                old_base = row['base']
                # Seed even unknown/conflicted bases: a later global version
                # stamp must not turn an unmerged canonical fix into local delta.
                snapshot = {'path': key, 'base': old_base}
                atomic(record, json.dumps(snapshot).encode())
                if row['status'] == 'MERGED':
                    backup = state / (ident + '.local')
                    if not safe(backup, root):
                        raise ValueError('unsafe override backup')
                    atomic(backup, decode(row['local']))
                    atomic(dst, decode(row['merged']), stat.S_IMODE(dst.stat().st_mode))
                if row['status'] in ('MERGED', 'CURRENT'):
                    snapshot['base'] = row['canonical']
                    atomic(record, json.dumps(snapshot).encode())
            # Include all three inputs and a marked candidate for review.
            # Never clear a conflict by accepting a new global install tag.
            row.pop('record', None)
            report.append(row)
            print('[update] OVERRIDE-%s %s: %s' %
                  (row['status'], key, row['reason']))
        if report:
            receipt = state / 'review.json'
            if not safe(receipt, root):
                raise ValueError('unsafe override review report')
            atomic(receipt, json.dumps({'owner': 'framework-maintainer', 'files': report},
                                      indent=2).encode())
            print('[update] override review items for framework-maintainer: %s (%d unresolved)' %
                  (receipt, sum(r['status'] == 'REVIEW' for r in report)))
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument('--override-plan', dest='root', required=True)
    parser.add_argument('--unverified-baseline', action='store_true')
    parser.add_argument('--confirm-baseline', action='store_true')
    for flag in ('subst', 'preserve', 'only', 'exclude', 'kit-layer'):
        parser.add_argument('--' + flag, action='append', default=[])
    parser.add_argument('specs', nargs='*')
    args = parser.parse_args(argv)
    root = Path(os.path.abspath(args.root))
    substs = [v.split('=', 1) for v in args.subst]
    only = [v.split(':', 1) for v in args.only]
    excludes = [v.split(':', 1) for v in args.exclude]
    rows, seen = [], set()

    def read(path, substitute=False):
        if path is None or not path.is_file() or path.is_symlink():
            return None
        data = current(path)
        if data is None:
            return None
        if substitute:
            for token, value in substs:
                data = data.replace(token.encode(), value.encode())
        return data

    for spec in args.specs:
        parts = spec.split('\t')
        surface, pay, live = parts[:3]
        base = Path(parts[3]) if len(parts) == 4 else None
        pay, live = Path(pay), Path(live)
        # Continuity documents, skills and owner state retain pin semantics.
        if surface.split('/')[0] not in (
                'bridge', 'routines', 'memory-engine', 'config', 'service',
                'database', 'mirror', 'scripts', 'git-hooks'):
            continue
        directory = pay.is_dir()
        names = {''}
        if directory:
            names = set()
            for tree in (pay, live, base):
                if tree is not None:
                    for folder, dirs, files in os.walk(tree):
                        dirs[:] = sorted(d for d in dirs
                                         if not (Path(folder) / d).is_symlink())
                        names.update(str((Path(folder) / f).relative_to(tree))
                                     for f in files)
        for rel in sorted(names):
            dst = live / rel if directory else live
            key = str(dst.relative_to(root))
            if key in seen or not preserved(key, args.preserve):
                continue
            seen.add(key)
            name = Path(rel or key).name
            if (excluded_builtin(surface, rel, name) or
                    flag_excluded(surface, rel, name, only, excludes)):
                continue
            src = pay / rel if directory else pay
            bf = base / rel if directory and base else base
            ident = hashlib.sha256(key.encode()).hexdigest()
            record = root / '.claude/override-reconcile' / (ident + '.json')
            rb = read(record) if safe(record, root) else None
            lb = read(dst) if safe(dst, root) else None
            pb = read(src, True)
            bb = decode(json.loads(rb)['base']) if rb is not None else read(bf, True)
            if rb is None and args.unverified_baseline and lb != bb:
                # The global install may have advanced while this fork was
                # held. It cannot establish the fork's original canonical base.
                bb = None
            if bb is None and args.confirm_baseline:
                bb = read(bf, True)
            row = dict(path=key, local=encode(lb) if lb is not None else None,
                       canonical=encode(pb) if pb is not None else None,
                       base=encode(bb) if bb is not None else None,
                       record=encode(rb) if rb is not None else None,
                       status='REVIEW', reason='missing or unverified fork baseline/file; copy held')
            if not safe(dst, root) or not safe(record, root):
                row['reason'] = 'symlink or unsafe path; copy held'
            elif lb is None and dst.is_file():
                row['reason'] = 'unreadable preserved file; copy held'
            elif lb is not None and pb is not None and lb == pb:
                row.update(status='CURRENT', reason='already canonical')
            elif bb is not None and pb is not None and lb is not None:
                if bb == pb:
                    row.update(status='CURRENT', reason='canonical unchanged; local delta retained')
                elif any(b'\0' in data for data in (bb, pb, lb)):
                    row['reason'] = 'binary content; review required'
                else:
                    with tempfile.TemporaryDirectory(prefix='dogany-override-') as tmp:
                        paths = [Path(tmp) / n for n in ('local', 'base', 'canonical')]
                        for path, data in zip(paths, (lb, bb, pb)):
                            path.write_bytes(data)
                        result = subprocess.run(
                            ['git', 'merge-file', '-p', '-L', 'local', '-L', 'base',
                             '-L', 'canonical', *map(str, paths)], capture_output=True)
                    if result.returncode == 0:
                        row.update(status='MERGED', reason='canonical changes rebased onto local delta',
                                   merged=encode(result.stdout))
                    else:
                        row.update(reason='overlapping canonical/local changes or merge error; copy held',
                                   candidate=encode(result.stdout))
            rows.append(row)
            print('[update] OVERRIDE-%s %s: %s (review owner: framework-maintainer)' %
                  (row['status'], key, row['reason']), file=sys.stderr)
    print(json.dumps({'root': str(root), 'files': rows}))
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1].startswith('--override-'):
        sys.exit(override_reconcile(sys.argv[1:]))
    sys.exit(main(sys.argv[1:]))
