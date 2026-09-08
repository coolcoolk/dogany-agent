#!/usr/bin/env python3
"""content_drift.py -- content-drift scanner, SINGLE SOURCE (DGN-1109 / DGN-1118).

One judgement, two callers (DGN-1034 shape guard: never fork this logic):
  * scripts/pack/pack_install.sh  -- reinstall lane (DGN-1109; dir specs, no
    baseline, no substitution -- byte-for-byte the behavior of the original
    embedded heredoc this file was extracted from).
  * update.sh                     -- framework-update lane (DGN-1118; adds
    baseline / substitution / preserve / only / exclude, all opt-in flags
    that leave the pack lane's semantics untouched when absent).

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
No baseline field -> strict DGN-1109 signal (pack lane behavior).

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

Exit: 0 = PASS, 3 = DRIFT only, 4 = any DESTRUCTIVE, 2 = usage/scan error
(callers treat unknown codes as scanner failure and fail closed, DGN-1004).
Output rows and the SUMMARY line are consumed verbatim by both callers.
"""

import fnmatch
import os
import re
import sys

PY_SYM = re.compile(r'^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)')
SH_SYM = re.compile(r'^(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)')


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
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--subst', '--preserve', '--only', '--exclude'):
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
            with open(src, 'rb') as f:
                pb = f.read()
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
            if basef is not None and os.path.isfile(basef) \
                    and not os.path.islink(basef):
                with open(basef, 'rb') as f:
                    btext = subst(f.read().decode('utf-8', 'replace'))
                if btext == ltext:
                    continue  # untouched vendor content: vendor progression
                blines = set(btext.splitlines())
                bsyms = symbols(rel, btext.splitlines())
            plines = ptext.splitlines()
            llines = ltext.splitlines()
            lost_set = (set(llines) - set(plines)) - blines
            lost_set.discard('')
            lost = len([l for l in lost_set if l.strip()])
            if lost == 0:
                continue
            net_shrink = len(llines) - len(plines)
            byte_halved = len(lb) >= 1024 and len(pb) * 2 < len(lb)
            syms_lost = sorted(
                (symbols(rel, llines) - symbols(rel, plines)) - bsyms)
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


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
