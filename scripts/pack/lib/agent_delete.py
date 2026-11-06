#!/usr/bin/env python3
"""DGN-1482 P1b: `mint_run.sh delete` -- one delete verb over the landed paths.

Design source: the DGN-1482 grill (findings 1, 3, 5, 6, 7, 11-14), which
supersedes the CRUD-SPEC for phase 1. This is a ROUTER, not new machinery:

  route      the DGN-1647 census decides (tests/dgn1647_lifecycle_census_selftest.py):
               teardown evidence TXN / FLOW / NEVER-RAN  -> mint_run.sh teardown
                 (never-ran or half-minted; retire refuses these, finding 13)
               evidence NONE (it ran)                     -> lib/mint_retire.py
             Their steps, receipts, resume and restore are reused as they are.
  additions  (made inside the retire plan itself, so a direct `retire` gets
             them too; see mint_retire.py + agent_residual.py)
               peers via _discover_instances (#5), mailbox pending gate keyed
               by SLUG (#6), generation check before a marker/receipt is
               honoured (#7), residual report (#3/#5). The router checks the
               mailbox for the teardown route itself.
  declared   a declared crew (crew.declared) membership is refused with the
  crews      reason and the crew_leave command (#14); --leave-declared-crews
             runs crew_leave first (apply only), then retires.
  units      a loaded unit or running process is refused by the routed verb,
             which prints the bootout lines (#11). Nothing here stops,
             boots out or kills anything.

Usage:
  mint_run.sh delete --root R (--dry-run | --apply) [--instance-root P]
          [--archive-dir D] [--mail-disposition archive] [--leave-declared-crews]
          [--managed-targets PATH] [--roster PATH]...
  mint_run.sh delete --resume RECEIPT [--dry-run] [--instance-root P]
  mint_run.sh delete --restore ARCHIVE [--dry-run] [--instance-root P]

stdout: ONE JSON object, schema "agent.delete/1" (route, census, rc, gate,
plan_additions, residual, report lines of the routed verb).
rc: the routed verb's own code, unchanged (finding 12). retire: 0 done,
2 request, 1 prepare, 3 other refusal, 6 partial. teardown: 0 done, 1 not a
target, 3 live, 4 no evidence, 5 lease, 6/8 not observed, 7 observer. The
router's own refusals use retire's codes (2 request, 3 refusal).
"""
import sys
sys.dont_write_bytecode = True

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess

LIB = Path(__file__).resolve().parent
PACK = LIB.parent
sys.path.insert(0, str(LIB))
import agent_residual  # noqa: E402
import mint_retire  # noqa: E402

SCHEMA = 'agent.delete/1'
TEARDOWN_EVIDENCE = ('TXN', 'FLOW', 'NEVER-RAN')
UNITS_POLICY = ('a loaded unit or running process is refused with its bootout line; '
                'delete never stops, boots out or kills anything')


def hint(gate):
    try:
        return json.loads((PACK / 'gates.json').read_text())['gates'][gate]['user_hint_ko']
    except (OSError, ValueError, KeyError):
        return None


def census(root, instance_root):
    """mint_teardown_evidence, the census's teardown half (TXN/FLOW/NEVER-RAN/NONE)."""
    p = subprocess.run(['bash', '-c', 'source "$1"; mint_teardown_evidence "$2" "$3"', 'census',
                        str(LIB / 'mint_teardown.sh'), str(root), instance_root or ''],
                       capture_output=True, text=True, timeout=60)
    ev = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ''
    return ev if p.returncode == 0 and ev in TEARDOWN_EVIDENCE + ('NONE',) else None


def declared_crews(root):
    """Kits whose crew.declared registry lists ROOT as a member."""
    crews = Path(os.environ.get('CREW_DIR', os.environ.get('DOGANY_CREW_DIR', str(
        mint_retire.shared_home() / 'crews'))))
    out = []
    for conf in sorted(crews.glob('*/crew.conf')):
        if not (conf.parent / 'crew.declared').exists():
            continue
        try:
            rows = [x[7:] for x in conf.read_text().splitlines() if x.startswith('member=')]
        except OSError:
            continue
        if any(agent_residual.same_root(x, root) for x in rows):
            out.append(conf.parent.name)
    return out


def crew_leave(root, kit, tag, dry):
    cmd = ['bash', str(PACK / 'crew_leave.sh'), 'leave', '--root', str(root), '--kit', kit, '--tag', tag]
    if dry:
        cmd.append('--dry-run')
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr).splitlines()


def run_retire(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = mint_retire.main(argv)
    return rc, out.getvalue().splitlines()


def run_teardown(argv):
    p = subprocess.run(['bash', str(PACK / 'mint_run.sh'), 'teardown'] + argv,
                       capture_output=True, text=True, timeout=600)
    return p.returncode, (p.stdout + p.stderr).splitlines()


def retire_gate(rc, lines):
    if rc == 0:
        return None
    first = lines[0].strip() if lines else ''
    return first if first.startswith('retire.') else 'retire.prepare'


def receipt_from(lines):
    for line in lines:
        m = re.match(r'Receipt: (.+RECEIPT\.json)$', line.strip())
        if m:
            return m[1]
    for line in reversed(lines):
        cand = line.strip()
        if cand.startswith('/') and (Path(cand) / 'RECEIPT.json').is_file():
            return str(Path(cand) / 'RECEIPT.json')
    return None


def result(mode, **kw):
    r = {'schema': SCHEMA, 'mode': mode, 'route': None, 'root': None, 'census': None,
         'rc': 0, 'gate': None, 'user_hint_ko': None, 'receipt': None,
         'plan_additions': {}, 'residual': None, 'units_policy': UNITS_POLICY, 'report': []}
    r.update(kw)
    if r['gate'] and not r['user_hint_ko']:
        r['user_hint_ko'] = hint(r['gate'])
    return r


def refuse(base, gate, detail, rc=3):
    base.update(rc=rc, gate=gate, user_hint_ko=hint(gate))
    base['report'].append(gate)
    base['report'].append('Detail: ' + detail)
    return base


def additions(root, conf, args):
    """Router-side view of the plan additions (all read-only)."""
    add = {}
    box = agent_residual.mailbox(root, conf)
    box['disposition'] = args.mail_disposition
    add['mailbox'] = box
    try:
        estate = mint_retire.estate_plan(root)
        add['peers'] = [{'conf': p['conf'], 'entries': p['entries'], 'found_by': p.get('found_by')}
                        for p in estate.get('peers', [])]
        add['discovery'] = estate.get('discovery')
        add['registry_rows'] = len((estate.get('registry') or {}).get('rows', []))
    except Exception as e:                            # reported; the routed verb re-judges
        add['peers'], add['discovery'] = [], {'error': str(e)}
    minted = conf.get('DOGANY_MINTED_AT')
    parent = Path(args.archive_dir).resolve() if args.archive_dir else root.parent / '.retired'
    current, other = agent_residual.receipts_for_root(root, parent, minted)
    try:
        ident = agent_residual.identity(root)
    except OSError:
        ident = None
    add['generation'] = {'minted_at': minted, 'identity': ident,
                         'receipts_this_generation': current,
                         'receipts_other_generations_ignored': other}
    return add


def delete_root(args):
    spelled = Path(args.root).absolute()
    mode = 'dry-run' if args.dry_run else 'apply'
    base = result(mode, root=str(spelled))
    if not os.path.lexists(spelled):
        base.update(route='none')
        base['report'].append('Nothing to delete: %s does not exist' % spelled)
        return base
    if spelled.is_symlink() or not spelled.is_dir():
        return refuse(base, 'retire.evidence', 'Target must be a physical instance directory: %s' % spelled)
    root = spelled.resolve()
    base['root'] = str(root)
    ev = census(root, args.instance_root)
    if ev is None:
        return refuse(base, 'retire.evidence', 'Census unavailable (mint_teardown_evidence failed)')
    route = 'teardown' if ev in TEARDOWN_EVIDENCE else 'retire'
    base['census'] = {'evidence': ev, 'route': route,
                      'rule': 'TXN/FLOW/NEVER-RAN -> teardown (never ran or half minted); '
                              'NONE (it ran) -> retire (DGN-1647 census)'}
    base['route'] = route
    try:
        conf = agent_residual.lenient_conf(root / '.instance.conf')
    except OSError as e:
        return refuse(base, 'retire.evidence', 'Cannot read .instance.conf: %s' % e)
    add = additions(root, conf, args)
    base['plan_additions'] = add
    try:
        peer_roots, _ = agent_residual.discovered_roots()
        base['residual'] = agent_residual.residual(root, conf, peer_roots, args.managed_targets,
                                                   args.roster or ())
    except Exception as e:
        return refuse(base, 'retire.evidence', 'Residual inventory failed: %s' % e)

    common = []
    if args.instance_root:
        common += ['--instance-root', args.instance_root]
    if args.archive_dir:
        common += ['--archive-dir', args.archive_dir]

    if route == 'teardown':
        box = add['mailbox']
        if agent_residual.mail_pending(box) and not args.mail_disposition:
            return refuse(base, 'retire.busy', 'Mailbox %s has pending mail (inbox %d, tmp %d); deliver it '
                          'first, or rerun with --mail-disposition archive' % (box['path'], box['inbox'], box['tmp']))
        rc, lines = run_teardown(['--root', str(root)] + common + (['--dry-run'] if args.dry_run else []))
        base.update(rc=rc, report=lines, gate=None if rc == 0 else 'teardown.rc%d' % rc)
        if rc == 0 and not args.dry_run:
            base['receipt'] = receipt_from(lines)
        base['report'] += agent_residual.residual_lines(base['residual'])
        return base

    kits = declared_crews(root)
    add['declared_crews'] = {'kits': kits, 'left': []}
    if kits:
        if not args.leave_declared_crews:
            return refuse(base, 'retire.evidence', 'Declared crew membership: %s -- leave it first: %s; '
                          'or rerun with --leave-declared-crews' % (', '.join(kits), '; '.join(
                              'scripts/pack/crew_leave.sh leave --root %s --kit %s' % (root, k) for k in kits)))
        tag = 'delete-' + (conf.get('DOGANY_AGENT_NAME') or root.name)
        for kit in kits:
            rc, lines = crew_leave(root, kit, tag, args.dry_run)
            add['declared_crews']['left'].append({'kit': kit, 'rc': rc, 'output': lines,
                                                  'dry_run': bool(args.dry_run)})
            if rc != 0:
                return refuse(base, 'retire.evidence', 'crew_leave refused for declared crew %s (rc %d): %s'
                              % (kit, rc, ' / '.join(lines[-3:])))
        if args.dry_run:
            base['report'].append('Dry run: declared crew membership (%s) would be left first; the '
                                  'retire dry run below still sees it' % ', '.join(kits))

    argv = ['--root', str(root)] + common
    if args.dry_run:
        argv.append('--dry-run')
    if args.mail_disposition:
        argv += ['--mail-disposition', args.mail_disposition]
    if args.managed_targets:
        argv += ['--managed-targets', args.managed_targets]
    for p in args.roster or ():
        argv += ['--roster', p]
    rc, lines = run_retire(argv)
    gate = retire_gate(rc, lines)
    base.update(rc=rc, gate=gate, user_hint_ko=hint(gate) if gate else None)
    base['report'] += lines
    base['receipt'] = receipt_from(lines)
    return base


def resume_or_restore(args):
    mode = 'restore' if args.restore else 'resume'
    target = Path(args.restore) / 'RECEIPT.json' if args.restore else Path(args.resume)
    base = result(mode + (' (dry-run)' if args.dry_run else ''), receipt=str(target))
    try:
        d = json.loads(target.read_text())
        assert isinstance(d, dict)
    except Exception as e:
        return refuse(base, 'retire.recovery', 'Cannot read receipt %s: %s' % (target, e))
    if 'transaction_id' in d or d.get('operation') in ('retire', 'restore'):
        base.update(route='retire', root=d.get('root'))
        argv = ['--restore', args.restore] if args.restore else ['--resume', args.resume]
        if args.instance_root:
            argv += ['--instance-root', args.instance_root]
        if args.dry_run:
            argv.append('--dry-run')
        rc, lines = run_retire(argv)
        gate = retire_gate(rc, lines)
        base.update(rc=rc, gate=gate, user_hint_ko=hint(gate) if gate else None, report=lines)
        return base
    base.update(route='teardown', root=d.get('original_root'))
    if not args.restore:
        return refuse(base, 'retire.request', 'A teardown receipt has no resume; restore it with --restore %s'
                      % target.parent, rc=2)
    if args.dry_run:
        base['report'].append('Dry run: would run mint_run.sh teardown --restore %s' % target.parent)
        return base
    argv = ['--restore', str(target.parent)]
    if args.instance_root:
        argv += ['--instance-root', args.instance_root]
    rc, lines = run_teardown(argv)
    base.update(rc=rc, report=lines, gate=None if rc == 0 else 'teardown.rc%d' % rc)
    return base


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse(argv):
    p = Parser(prog='mint_run.sh delete', add_help=False)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--root'); target.add_argument('--resume'); target.add_argument('--restore')
    p.add_argument('--dry-run', action='store_true'); p.add_argument('--apply', action='store_true')
    p.add_argument('--instance-root'); p.add_argument('--archive-dir')
    p.add_argument('--mail-disposition', choices=['archive'])
    p.add_argument('--leave-declared-crews', action='store_true')
    p.add_argument('--managed-targets'); p.add_argument('--roster', action='append')
    args = p.parse_args(argv)
    if args.dry_run and args.apply:
        raise ValueError('--dry-run and --apply are exclusive')
    if args.root and not (args.dry_run or args.apply):
        raise ValueError('--root needs --dry-run or --apply')
    if not args.root and (args.archive_dir or args.leave_declared_crews or args.mail_disposition):
        raise ValueError('--archive-dir/--leave-declared-crews/--mail-disposition need --root')
    return args


def main(argv=None):
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
    except ValueError as e:
        out = refuse(result('usage'), 'retire.request', str(e) + ' -- ' + __doc__.split('Usage:')[1]
                     .split('stdout:')[0].strip().replace('\n', ' '), rc=2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2
    out = delete_root(args) if args.root else resume_or_restore(args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out['rc']


if __name__ == '__main__':
    sys.exit(main())
