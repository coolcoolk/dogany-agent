#!/usr/bin/env python3
"""DGN-1482 P1a: `dogany agent list` -- every agent on this machine, read-only.

One row per live instance root and per archive receipt, so an operator sees
the whole estate before delete/purge (P1b/P1c) exist. Design source: the
DGN-1482 grill (findings 2, 7, 8), which supersedes the CRUD-SPEC.

Sources (all read, none written):
  instances   _discover_instances (discover_instances.sh) -- the same scan
              update_plan.sh uses; its candidate list is re-checked here so an
              UNREADABLE root becomes an error row instead of silently
              vanishing (finding 8: the library drops it by design)
  scan dirs   the fixed-depth estate dirs that scan reads; an unreadable one
              is an error row too
  archives    <instance parent>/.retired/*/RECEIPT.json (mint_retire.py) and
              <shared home>/torn-down/*/RECEIPT.json (teardown), one row per
              receipt, every non-complete receipt shown as such (finding 7)
  launchd     one print of the gui/<uid> domain, enumerated like
              mint_retire.py service_labels, through the launch-host library
              (launch_host.launchd_domain_services, v2.7 T6 seam); a failed or
              unparseable answer -> every live/stopped verdict is UNKNOWN,
              never "not loaded" (finding 8: `dogany status` reads a failed
              domain query as NOT loaded). Label + host (lease) resolution are
              the library's too (resolve_label / resolve_host).
  crews       <crews home>/*/crew.conf member= lines
  ledger      <root>/config/minting-state.json (the parent's minting ledger)
  retiring    retire_marker.state(root), honoured only when the receipt's
              generation matches the root's (finding 7: one root path has
              carried several generations)
  MANAGED-TARGETS  only when --managed-targets PATH is given (a dev-crew sot
              doc; framework code never assumes where it lives)

Row: name, root, kind (instance|archive|record), class, state, generation,
errors (+ supporting fields in --json). States:
  live       main service label loaded with a running pid
  stopped    readable instance, main label not running / not loaded
  retiring   a retirement holds this generation (marker + receipt)
  partial    half-minted root (.mint-incomplete), or a receipt that stopped
             partway (partial / restore-partial / prepared / retiring / restoring)
  retired    complete (or restored-and-spent) retire receipt
  torn-down  complete teardown receipt
  unknown    could not be read; the row says why
Generation = "<DOGANY_MINTED_AT>/<root inode>" (the identity mint_retire.py
records; archives keep the inode because they are moved by rename).

Exit: 0 every row known; 5 at least one unknown row; 2 usage.
Read-only: no file is written, not even bytecode (dont_write_bytecode).
Test seams: DOGANY_DISCOVER_INSTANCES_OVERRIDE (discovery), DOGANY_LAUNCHCTL
(the launch-host library's service-manager binary), DOGANY_SHARED_HOME,
DOGANY_CREW_DIR.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))
import retire_marker  # noqa: E402
sys.path.insert(0, str(LIB.parents[2] / 'agents/.template/routines/lib'))
import launch_host  # noqa: E402
import agent_name  # noqa: E402

RC_UNKNOWN = 5
RETIRE_STATE = {'complete': 'retired', 'restored': 'retired',
                'partial': 'partial', 'restore-partial': 'partial',
                'prepared': 'partial', 'retiring': 'partial', 'restoring': 'partial'}
TEARDOWN_DONE = {'complete', 'retired'}


def home():
    return Path(os.environ.get('HOME', ''))


def shared_home():
    return Path(os.environ.get('DOGANY_SHARED_HOME') or str(home() / '.dogany'))


def crew_home():
    return Path(os.environ.get('DOGANY_CREW_DIR') or str(shared_home() / 'crews'))


def read_conf(path):
    """KEY=value lines; raises OSError when unreadable."""
    result = {}
    for line in Path(path).read_text(errors='replace').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            result.setdefault(k.strip(), v.strip())
    return result


def generation(minted_at, ino):
    return '%s/%s' % (minted_at or '?', ino if ino is not None else '?')


def row(name, root, kind, state='unknown', **extra):
    r = {'name': name, 'root': str(root), 'kind': kind, 'class': '-', 'state': state,
         'generation': '-', 'errors': [], 'notes': []}
    r.update(extra)
    return r


# ---------------------------------------------------------------- discovery

DISCOVER_SH = LIB / 'discover_instances.sh'


def discover():
    """(instances [(name, root)], candidates [path], error or None)."""
    script = ('source "$1"; _discover_instances; rc=$?; echo "@@rc=$rc"; '
              '_discover_instance_candidates')
    try:
        p = subprocess.run(['bash', '-c', script, 'agent_list', str(DISCOVER_SH)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return [], [], 'discovery failed: %s' % e
    found, cands, rc, seen_rc = [], [], 0, False
    for line in p.stdout.splitlines():
        if line.startswith('@@rc='):
            seen_rc, rc = True, int(line[5:] or 1)
            continue
        if not seen_rc:
            if ':' in line:
                name, root = line.split(':', 1)
                found.append((name, root))
        elif line.strip():
            cands.append(line.strip())
    if not seen_rc or rc != 0:
        return found, cands, 'discovery rc %s (HOME unusable?)' % (rc if seen_rc else p.returncode)
    return found, cands, None


def scan_dirs():
    """The fixed-depth dirs discover_instances.sh globs, existing ones only."""
    h, s = home(), shared_home()
    dirs = [s / 'agents', h / 'dogany']
    try:
        for d in sorted((h / 'dogany').iterdir()):
            for sub in ('agents', 'poc'):
                if (d / sub).is_dir():
                    dirs.append(d / sub)
    except OSError:
        pass
    return [d for d in dirs if os.path.lexists(d)]


def probe_root(path):
    """'instance' | 'absent' | error string, for one candidate root."""
    try:
        os.stat(Path(path) / '.instance.conf')
    except FileNotFoundError:
        return 'absent'
    except NotADirectoryError:
        return 'absent'
    except PermissionError as e:
        return 'unreadable: %s' % e.strerror
    except OSError as e:
        return 'unreadable: %s' % e
    return 'instance' if os.access(path, os.R_OK | os.X_OK) else 'unreadable: root not traversable'


# ---------------------------------------------------------------- launchd

def launchd_domain():
    """{'domain', 'status': ok|unknown, 'services': {label: pid or None}, 'error'}."""
    services, error = launch_host.launchd_domain_services()
    return {'domain': 'gui/%d' % os.getuid(), 'status': 'unknown' if services is None else 'ok',
            'services': services or {}, 'error': error}


# ---------------------------------------------------------------- rows

def instance_row(name, root, launchd):
    r = row(name, root, 'instance')
    try:
        c = read_conf(Path(root) / '.instance.conf')
    except OSError as e:
        r['errors'].append('.instance.conf unreadable: %s' % (e.strerror or e))
        return r
    r['class'] = c.get('DOGANY_AGENT_CLASS') or '-'
    r['agent_name'] = c.get('DOGANY_AGENT_NAME') or None
    r['display_name'] = agent_name.display_name(str(root))
    try:
        ino = os.stat(root).st_ino
    except OSError:
        ino = None
    r['generation'] = generation(c.get('DOGANY_MINTED_AT'), ino)
    r['identity_ino'] = ino

    label = launch_host.resolve_label(root)[0] or None
    host = launch_host.resolve_host(root)
    r['label'], r['host'] = label, host['host']
    r['notes'].extend(host['warnings'])
    if host['host'] == 'app':
        # Bridge hosted by the app (lease): launchd holds no bridge pid.
        holder = launch_host.read_lock_holder(root)
        state = 'live' if holder and holder['alive'] else 'stopped'
        r['notes'].append('bridge host = app (lease)')
    elif launchd['status'] != 'ok':
        state = 'unknown'
        r['errors'].append('launchd unknown: ' + (launchd['error'] or '?'))
    elif label is None:
        state = 'stopped'
        r['notes'].append('no service label found')
    else:
        svc = launchd['services']
        units = [x for x in svc if x == label or x.startswith(label + '.')]
        r['units_loaded'] = len(units)
        r['pid'] = svc.get(label)
        state = 'live' if svc.get(label) else 'stopped'

    if os.path.lexists(Path(root) / '.mint-incomplete'):
        state = 'partial'
        r['notes'].append('.mint-incomplete present (half-minted)')
    marker = retire_marker.state(root)
    if marker is not None:
        held = marker_generation_matches(marker, ino)
        if held is False:
            r['notes'].append('retire marker names another generation (ignored): %s'
                              % marker.get('receipt'))
        else:
            state = 'partial' if marker.get('status') in ('partial', 'restore-partial') else 'retiring'
            r['retire_receipt'] = marker.get('receipt')
            r['notes'].append('retire marker: %s' % (marker.get('status') or marker.get('why') or 'held'))
    r['state'] = state

    try:
        parent = json.loads((Path(root) / 'config/minting-parent.json').read_text())
        r['minting_parent'] = parent.get('state')
    except (OSError, ValueError, AttributeError):
        pass
    return r


def marker_generation_matches(marker, ino):
    """True / False when the marker's receipt carries a generation, else None."""
    receipt = marker.get('receipt')
    if not receipt:
        return None
    try:
        gen = json.loads(Path(receipt).read_text()).get('generation') or {}
        return list(gen['identity'])[1] == ino
    except Exception:
        return None


def short_id(dirname):
    """health.5603d5bd-e6c2-... -> health.5603d5bd (table display only)."""
    m = re.fullmatch(r'(.+)\.([0-9a-f]{8})-[0-9a-f-]{27}', dirname)
    return m[1] + '.' + m[2] if m else dirname


def archive_row(receipt_path, kind_hint):
    adir = receipt_path.parent
    r = row(adir.name, '-', 'archive', archive=str(adir), archive_kind=kind_hint,
            receipt=str(receipt_path))
    try:
        d = json.loads(receipt_path.read_text())
        assert isinstance(d, dict)
    except Exception as e:
        r['errors'].append('RECEIPT.json unreadable: %s' % (getattr(e, 'strerror', None) or e))
        return r
    status = d.get('status')
    r['receipt_status'] = status
    if 'transaction_id' in d or d.get('operation') == 'retire':
        r['archive_kind'] = 'retired'
        r['agent_name'] = d.get('name')
        r['root'] = d.get('root') or '-'
        r['created_at'] = d.get('created_at')
        gen = d.get('generation') or {}
        ident = gen.get('identity') or [None, None]
        r['generation'] = generation(gen.get('minted_at'), ident[1] if len(ident) > 1 else None)
        state = RETIRE_STATE.get(status)
        cause = d.get('cause') or {}
        if cause.get('gate'):
            r['notes'].append('cause %s: %s' % (cause.get('gate'), cause.get('detail', '')))
    else:
        r['archive_kind'] = 'torn-down'
        r['agent_name'] = d.get('slug')
        r['root'] = d.get('original_root') or '-'
        r['created_at'] = d.get('torn_down_at') or d.get('retired_at')
        body = adir / 'instance'
        try:
            minted = read_conf(body / '.instance.conf').get('DOGANY_MINTED_AT')
        except OSError:
            minted = None
        try:
            ino = os.stat(body).st_ino
        except OSError:
            ino = None
        r['generation'] = generation(minted, ino)
        state = 'torn-down' if status in TEARDOWN_DONE else ('partial' if status else None)
    if state is None:
        r['errors'].append('unrecognized receipt status: %r' % (status,))
        state = 'unknown'
    r['state'] = state
    return r


def archive_dirs(instance_roots):
    """(.retired dirs, torn-down dirs) to read, de-duplicated, existing only."""
    retired = [shared_home() / 'agents/.retired']
    torn = [shared_home() / 'torn-down']
    for root in instance_roots:
        parent = Path(root).parent
        retired.append(parent / '.retired')
        torn.append(parent.parent / 'torn-down')
    out = []
    for kind, dirs in (('retired', retired), ('torn-down', torn)):
        seen = set()
        for d in dirs:
            key = os.path.realpath(d)
            if key in seen or not os.path.lexists(d):
                continue
            seen.add(key)
            out.append((kind, d))
    return out


def crews():
    """{physical root: [kit, ...]} plus the conf files read and errors."""
    members, read, errors = {}, [], []
    try:
        confs = sorted(crew_home().glob('*/crew.conf'))
    except OSError as e:
        return members, read, ['crews dir unreadable: %s' % e]
    for conf in confs:
        kit = conf.parent.name
        try:
            lines = conf.read_text().splitlines()
        except OSError as e:
            errors.append('%s unreadable: %s' % (conf, e.strerror or e))
            continue
        read.append(str(conf))
        for line in lines:
            if line.startswith('member='):
                p = line[len('member='):].strip()
                members.setdefault(os.path.realpath(p), []).append(kit)
    return members, read, errors


def managed_targets(path):
    """Backticked absolute paths in a MANAGED-TARGETS.md table -> slug."""
    out = {}
    for line in Path(path).read_text(errors='replace').splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        for c in cells:
            for p in re.findall(r'`(/[^`]+)`', c):
                out.setdefault(os.path.realpath(p), cells[0])
    return out


# ---------------------------------------------------------------- build

def build(managed_path=None):
    rows, sources = [], []
    found, cands, derr = discover()
    sources.append({'source': 'discover_instances', 'path': str(DISCOVER_SH),
                    'status': 'error' if derr else 'ok', 'found': len(found),
                    **({'error': derr} if derr else {})})
    if derr and not found:
        rows.append(row('-', home(), 'record', errors=[derr]))

    for d in scan_dirs():
        try:
            n = len(os.listdir(d))
            sources.append({'source': 'scan-dir', 'path': str(d), 'status': 'ok', 'entries': n})
        except OSError as e:
            sources.append({'source': 'scan-dir', 'path': str(d), 'status': 'unreadable'})
            rows.append(row(Path(d).name, d, 'record',
                            errors=['scan dir unreadable: %s' % (e.strerror or e)]))

    launchd = launchd_domain()
    sources.append({'source': 'launchd', 'path': launchd['domain'], 'status': launchd['status'],
                    'services': len(launchd['services']),
                    **({'error': launchd['error']} if launchd['error'] else {})})

    by_root = {}
    for name, root in found:
        by_root[os.path.realpath(root)] = instance_row(name, os.path.realpath(root), launchd)

    crew_members, crew_read, crew_err = crews()
    sources.append({'source': 'crew.conf', 'path': str(crew_home()), 'status':
                    'error' if crew_err else 'ok', 'files': crew_read,
                    **({'error': '; '.join(crew_err)} if crew_err else {})})

    # Candidates the library dropped: an unreadable one is an error row; a
    # crew member discovery could not see is checked the same way.
    stale = []
    for c in cands + list(crew_members):
        real = os.path.realpath(c.rstrip('/'))
        if real in by_root:
            continue
        verdict = probe_root(real)
        if verdict == 'instance' and real in crew_members:
            r = instance_row(Path(real).name, real, launchd)
            r['notes'].append('found only through crew.conf')
            by_root[real] = r
        elif verdict.startswith('unreadable'):
            by_root[real] = row(Path(real).name, real, 'instance', errors=['root ' + verdict])
        elif real in crew_members:
            stale.append(real)
            rows.append(row(Path(real).name, real, 'record',
                            errors=['crew.conf member is not an instance (%s)' % verdict]))
    for real, r in by_root.items():
        r['crews'] = crew_members.get(real, [])

    # Minting ledgers (read from every instance root, parent side).
    ledgers, dangling = [], []
    for real, r in list(by_root.items()):
        path = Path(real) / 'config/minting-state.json'
        try:
            if not path.exists():
                continue
            state = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            r['errors'].append('minting-state.json unreadable: %s' % e)
            continue
        ledgers.append(str(path))
        for domain, entry in (state.get('domains') or {}).items():
            agent = (entry or {}).get('dedicated_agent')
            if not agent:
                continue
            target = os.path.realpath(entry.get('agent_path') or '')
            if entry.get('agent_path') and target in by_root:
                by_root[target].setdefault('minted_by', []).append('%s:%s' % (r['name'], domain))
            else:
                dangling.append({'ledger': str(path), 'domain': domain, 'agent': agent,
                                 'agent_path': entry.get('agent_path')})
    sources.append({'source': 'minting-state', 'status': 'ok', 'files': ledgers})

    if managed_path:
        try:
            mt = managed_targets(managed_path)
            sources.append({'source': 'MANAGED-TARGETS', 'path': str(managed_path),
                            'status': 'ok', 'paths': len(mt)})
            for real, r in by_root.items():
                r['managed_target'] = real in mt
        except OSError as e:
            sources.append({'source': 'MANAGED-TARGETS', 'path': str(managed_path),
                            'status': 'unreadable', 'error': str(e)})

    archives = []
    for kind, d in archive_dirs(list(by_root)):
        try:
            entries = sorted(os.listdir(d))
        except OSError as e:
            sources.append({'source': kind + '-archives', 'path': str(d), 'status': 'unreadable'})
            rows.append(row(Path(d).name, d, 'record',
                            errors=['archive dir unreadable: %s' % (e.strerror or e)]))
            continue
        n = 0
        for e in entries:
            if not (Path(d) / e).is_dir():
                continue
            receipt = Path(d) / e / 'RECEIPT.json'
            if not os.path.lexists(receipt):
                a = row(e, '-', 'archive', archive=str(Path(d) / e), archive_kind=kind)
                a['errors'].append('archive has no RECEIPT.json')
                archives.append(a)
            else:
                archives.append(archive_row(receipt, kind))
            n += 1
        sources.append({'source': kind + '-archives', 'path': str(d), 'status': 'ok', 'receipts': n})

    instance_rows = sorted(by_root.values(), key=lambda r: (r['name'].lower(), r['root']))
    names = {}
    for r in instance_rows:
        names.setdefault(r['name'], []).append(r['root'])
    for r in instance_rows:
        if len(names[r['name']]) > 1:
            r['notes'].append('name shared with ' + ', '.join(x for x in names[r['name']] if x != r['root']))
    archives.sort(key=lambda r: (r.get('archive_kind') or '', r['name']))
    rows = instance_rows + archives + rows
    unknown = sum(1 for r in rows if r['state'] == 'unknown')
    return {
        'schema': 1,
        'count': len(rows),
        'instances': len(instance_rows),
        'archives': len(archives),
        'unknown': unknown,
        'rc': RC_UNKNOWN if unknown else 0,
        'scanned_sources': sources,
        'ledger_dangling': dangling,
        'rows': rows,
    }


def table(result):
    out = []
    head = ('NAME', 'KIND', 'CLASS', 'STATE', 'GENERATION', 'ROOT')
    body = []
    for r in result['rows']:
        name = short_id(r['name']) if r['kind'] == 'archive' else r.get('display_name', r['name'])
        kind = r.get('archive_kind') or r['kind'] if r['kind'] == 'archive' else r['kind']
        state = r['state']
        if r.get('receipt_status') and r['receipt_status'] not in ('complete',):
            state += ' (%s)' % r['receipt_status']
        body.append((name, kind, r['class'], state, r['generation'], r['root']))
    widths = [max(len(x[i]) for x in [head] + body) for i in range(len(head) - 1)]
    for line in [head] + body:
        out.append('  '.join(c.ljust(w) for c, w in zip(line, widths)) + '  ' + line[-1])
    for r in result['rows']:
        for e in r['errors']:
            out.append('error: %s (%s): %s' % (r['name'], r['root'], e))
    out.append('%d rows: %d instances, %d archives, %d unknown' % (
        result['count'], result['instances'], result['archives'], result['unknown']))
    tally = {}
    for s in result['scanned_sources']:
        tally.setdefault(s['source'], []).append(s['status'])
    out.append('scanned: ' + ', '.join(
        '%s=%s' % (src, ','.join('%d %s' % (st.count(x), x) for x in sorted(set(st)))
                   if len(st) > 1 else st[0]) for src, st in tally.items()))
    for d in result['ledger_dangling']:
        out.append('ledger: %s names %s at %s (not a listed instance)' % (
            d['domain'], d['agent'], d['agent_path']))
    return '\n'.join(out)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print('agent list: ' + message, file=sys.stderr)
        sys.exit(2)


def main(argv=None):
    ap = Parser(prog='dogany agent list',
                description='List every agent instance and archive on this machine (read-only).')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--managed-targets', metavar='PATH',
                    help='also mark which roots a MANAGED-TARGETS.md names')
    args = ap.parse_args(argv)
    result = build(args.managed_targets)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(table(result))
    return result['rc']


if __name__ == '__main__':
    sys.exit(main())
