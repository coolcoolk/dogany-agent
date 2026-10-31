#!/usr/bin/env python3
"""DGN-1624 minimal retirement. No lifecycle locks, service mutations or pushes.

Observer read/modify/write deliberately retains the accepted stale-writer and
queued-notification races. Readback retries once; it is not a concurrency fence.
Crew helpers retain their existing per-file locking; no new protocol is added.
DGN-1786 adds one exclusion with /update (retire_marker.py): a run marks the
agent as being retired before it looks for an update in flight, and keeps the
mark while the retirement is in progress, so an update skips the agent instead
of bringing it back; an update already applying refuses this verb up front.
OS dependencies are injectable like mint_teardown's probes, for scratch tests.
"""
import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))
import retire_marker
import transcript_store

MARKS = ['logs/bot.log', 'poll_heartbeat', 'sessions.json',
         'runtime-snapshot.json', '.service_plist']
STATES = {'prepared', 'retiring', 'partial', 'complete', 'restoring',
          'restore-partial', 'restored'}


class Refusal(Exception):
    def __init__(self, gate, detail):
        self.gate, self.detail = 'retire.' + gate, str(detail)
        super().__init__(self.detail)


def require(ok, gate, detail):
    if not ok:
        raise Refusal(gate, detail)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(path):
    s = Path(path).stat()
    return [s.st_dev, s.st_ino]


def exists(path):
    return os.path.lexists(path)


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def syncdir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.retire-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        syncdir(path.parent)
    finally:
        if exists(tmp):
            os.unlink(tmp)


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=45)


def conf(path):
    result = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as e:
        raise Refusal('evidence', 'Cannot read configuration: ' + str(path)) from e
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        require('=' in line, 'evidence', 'Malformed configuration: ' + str(path))
        k, v = line.split('=', 1)
        require(re.fullmatch(r'[A-Z][A-Z0-9_]*', k) and k not in result,
                'evidence', 'Ambiguous configuration: ' + str(path))
        result[k] = v
    return result


def packs(c):
    result = {}
    for item in filter(None, c.get('DOGANY_PACKS', '').split(',')):
        require(re.fullmatch(r'[A-Za-z0-9_-]+@[^\s,@]+', item),
                'evidence', 'Malformed installed pack entry')
        k, v = item.split('@')
        require(k not in result, 'evidence', 'Duplicate installed pack entry')
        result[k] = v
    return result


def tree_hash(root):
    """Hash files and links without dereferencing shared DB links or tokens."""
    root = Path(root)
    h = hashlib.sha256()
    paths = [root] + sorted(root.rglob('*')) if root.is_dir() else [root]
    for p in paths:
        st = p.lstat()
        h.update(str(p.relative_to(root)).encode())
        h.update(str(st.st_mode).encode())
        if p.is_symlink():
            h.update(os.readlink(p).encode())
        elif p.is_file():
            h.update(p.read_bytes())
        else:
            require(p.is_dir(), 'evidence', 'Non-regular body object: ' + str(p))
    return h.hexdigest()


def runtime(root):
    found = []
    for rel in MARKS:
        p = Path(root) / '.telegram_bot' / rel
        if not exists(p):
            continue
        require(p.is_file() and not p.is_symlink() and inside(p, root),
                'evidence', 'Unsafe runtime artifact: ' + str(p))
        data = p.read_text()
        if not data.strip():
            continue
        if rel.endswith('.json'):
            require(isinstance(json.loads(data), (dict, list)), 'evidence', 'Invalid runtime JSON')
        elif rel == 'poll_heartbeat':
            # Bridge variants record either epoch seconds or an ISO timestamp.
            try:
                float(data.strip())
            except ValueError:
                dt.datetime.fromisoformat(data.strip().replace('Z', '+00:00'))
        elif rel == '.service_plist':
            # DGN-1633: the record is TWO lines -- absolute plist path, then the
            # bare launchd label. Reading it with a single .strip() glued both
            # together and refused every real instance (all three live ones
            # carry two lines). Parse the path from line 1; the label line, when
            # present, must be a bare label and never a second path.
            recorded = service_plist_path(data)
            require(recorded is not None,
                    'evidence', 'Service-plist record does not identify a readable plist')
            if recorded.is_file():
                plistlib.loads(recorded.read_bytes())
        elif rel == 'logs/bot.log':
            require(bool(re.search(r'\d{4}-\d\d-\d\d.*\S', data)),
                    'evidence', 'Bot log lacks timestamped runtime evidence')
        found.append({'kind': rel, 'digest': digest(p), 'size': p.stat().st_size,
                      'mtime_ns': p.stat().st_mtime_ns})
    return found


def completed(root, caller):
    require(root.is_dir() and not root.is_symlink() and root.stat().st_uid == os.getuid(),
            'evidence', 'Root must be an owned, physical instance directory')
    require(root not in (Path('/'), Path.home().resolve(), LIB.parents[2]) and
            not (caller and root == Path(caller).resolve()), 'evidence', 'Unsafe/self target')
    for key in ('DOGANY_INSTANCE_ROOT', 'DOGANY_ROOT'):
        require(not os.environ.get(key) or root != Path(os.environ[key]).resolve(),
                'evidence', 'Refusing own instance')
    require(not inside(Path.cwd(), root), 'evidence', 'Target contains the calling working directory')
    require(not any(p.parent != root for p in root.rglob('.instance.conf')),
            'evidence', 'Shared body contains another instance manifest')
    require(not exists(root / '.mint-incomplete'), 'evidence', 'TXN: .mint-incomplete exists')
    c = conf(root / '.instance.conf')
    for k in ('DOGANY_AGENT_NAME', 'DOGANY_MINTED_AT', 'DOGANY_FW_VERSION',
              'DOGANY_FW_REPO_SHA', 'DOGANY_REPO_ROOT'):
        require(bool(c.get(k)), 'evidence', 'Missing minted identity: ' + k)
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]+', c['DOGANY_AGENT_NAME'])),
            'evidence', 'Invalid agent name')
    try:
        dt.datetime.fromisoformat(c['DOGANY_MINTED_AT'].replace('Z', '+00:00'))
    except ValueError as e:
        raise Refusal('evidence', 'Invalid mint timestamp') from e
    require(bool(re.fullmatch(r'[0-9a-fA-F]{7,40}', c['DOGANY_FW_REPO_SHA'])),
            'evidence', 'Invalid framework SHA')
    packs(c)
    for p in (root / 'config/agent.conf', root / '.telegram_bot/config/agent.conf'):
        if p.is_file():
            other = conf(p)
            require(other.get('DOGANY_AGENT_NAME', c['DOGANY_AGENT_NAME']) == c['DOGANY_AGENT_NAME'],
                    'evidence', 'Body identity disagrees with manifest')
    try:
        evidence = runtime(root)
    except (ValueError, OSError, TypeError) as e:
        raise Refusal('evidence', 'Invalid prior runtime evidence: ' + str(e)) from e
    require(evidence, 'evidence', 'NEVER-RAN: no valid prior runtime artifact')
    return c, evidence


def service_plist_path(text):
    """First line of a .service_plist record -> absolute plist Path, else None.

    DGN-1633: the on-disk record written by the bridge install path is two
    lines, `<absolute plist path>\n<launchd label>`. Any reader that strips the
    whole blob sees one glued string that ends in the label, not in `.plist`.
    A trailing label line is accepted and ignored here; a SECOND path is not --
    that would be an ambiguous record, and guessing which one is live is
    exactly the class of assumption this verb refuses to make.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = Path(lines[0])
    if not (first.is_absolute() and lines[0].endswith('.plist')):
        return None
    for extra in lines[1:]:
        if extra.startswith('/') or extra.endswith('.plist'):
            return None
    return first


def plist_belongs(d, root):
    """True unless this plist demonstrably belongs to a DIFFERENT instance.

    DGN-1634: the first cut required WorkingDirectory to sit inside the
    instance root. No real instance satisfies that -- every live newbridge
    plist in this estate sets WorkingDirectory to the operator's home, and the
    retire verb therefore refused every genuine agent while its own fixture,
    which wrote WorkingDirectory=<root>, passed. The fixture described a world
    that does not exist.

    The load-bearing identity signal is the path the unit is told to RUN, not
    the directory it happens to start in. So: if any program argument names an
    instance root, it must be THIS root. A plist that names no root at all is
    accepted here -- the caller's Label/owner checks still apply -- because a
    missing signal is not evidence of a foreign owner.
    """
    args = d.get('ProgramArguments') or []
    named = [a for a in args if isinstance(a, str) and '/.dogany/agents/' in a
             or isinstance(a, str) and '/agents/' in a and a.startswith('/')]
    for a in named:
        if inside(a, root) or Path(a).resolve() == Path(root).resolve():
            return True
    if named:
        return False
    wd = d.get('WorkingDirectory')
    if wd and inside(wd, root):
        return True
    return not named


def plist_inventory(root):
    candidates = list((root / 'bridge').glob('*.plist')) + list((root / 'routines').glob('*.plist'))
    record = root / '.telegram_bot/.service_plist'
    if record.is_file():
        recorded = service_plist_path(record.read_text())
        require(recorded is not None,
                'evidence', 'Service-plist record does not identify a readable plist')
        candidates.append(recorded)
    external = Path(os.environ.get('DOGANY_LAUNCH_AGENTS_DIR', str(Path.home() / 'Library/LaunchAgents')))
    selected = {}
    for p in sorted(set(candidates)):
        d = plistlib.loads(p.read_bytes())
        label = d.get('Label')
        require(plist_belongs(d, root),
                'evidence', 'Plist points at a different instance: ' + str(p))
        require(isinstance(label, str) and label and '/' not in label and '\n' not in label,
                'evidence', 'Unresolved plist Label: ' + str(p))
        selected.setdefault(label, []).append({'path': str(p), 'digest': digest(p), 'identity': identity(p)})
    own_labels = set(selected)
    for p in sorted(external.glob('*.plist')):
        try:
            d = plistlib.loads(p.read_bytes())
        except Exception:
            # DGN-1635: the install directory is SHARED by every instance on
            # this machine, and unrelated units there can be unparseable (nine
            # were, all carrying `--` inside an XML comment, which launchd
            # tolerates and plistlib does not). Dying on a foreign broken file
            # made this verb unusable for reasons that have nothing to do with
            # the agent being retired. A file whose NAME carries one of this
            # instance's labels still fails closed -- that one is ours, and an
            # unreadable unit of ours is exactly what we must not guess about.
            require(not any(p.name.startswith(lbl) for lbl in own_labels),
                    'evidence', 'Installed plist for this instance is unreadable: ' + str(p))
            continue
        vals = [d.get('WorkingDirectory', ''), d.get('Program', '')] + d.get('ProgramArguments', [])
        owns = any(isinstance(v, str) and v.startswith('/') and inside(v, root) for v in vals)
        if owns or d.get('Label') in selected:
            label = d.get('Label')
            require(isinstance(label, str) and label and '/' not in label,
                    'evidence', 'Unresolved installed Label')
            # A same-Label installed plist pointing at a DIFFERENT instance is
            # contradictory; one that merely starts in $HOME is not (DGN-1634).
            require(plist_belongs(d, root), 'evidence', 'Shared/contradictory service ownership')
            selected.setdefault(label, []).append({'path': str(p), 'digest': digest(p), 'identity': identity(p)})
    return selected


def installed_plist_paths(root):
    """Installed LaunchAgents plists owned by *root*, by their real paths.

    `mint_run.sh start` copies plist files by filename, not by Label.  Keep
    callers that need to clean up installed copies on this inventory instead
    of reconstructing a filename from a label (the bridge's `.newbridge`
    variant is the important counterexample).
    """
    external = Path(os.environ.get('DOGANY_LAUNCH_AGENTS_DIR',
                                   str(Path.home() / 'Library/LaunchAgents')))
    external_real = external.resolve()
    paths = set()
    for entries in plist_inventory(Path(root)).values():
        for entry in entries:
            path = Path(entry['path'])
            if path.parent.resolve() == external_real:
                paths.add(path)
    return sorted(paths)


def ctl(*args):
    return run([os.environ.get('MINT_RETIRE_LAUNCHCTL', 'launchctl'), *args])


def service_labels(domain):
    p = ctl('print', domain)
    require(p.returncode == 0, 'evidence', 'Cannot inspect launchd domain ' + domain + ': ' + p.stderr)
    require(p.stdout.startswith(domain + ' = {'), 'evidence', 'Unrecognized domain response: ' + domain)
    block = re.search(r'\n\s*services = \{\n(.*?)\n\s*\}', p.stdout, re.S)
    require(block is not None, 'evidence', 'Cannot enumerate exact domain Labels: ' + domain)
    labels = []
    for line in block[1].splitlines():
        if not line.strip():
            continue
        # DGN-1636: the status column is not always numeric. Real macOS emits a
        # parenthesised reason token for many services -- `(pe)`, `(jt)` -- and
        # the numeric-only pattern refused the whole domain on the first such
        # row. This machine had them in 2246 rows of one domain, so the verb
        # could never enumerate a real user domain at all.
        m = re.fullmatch(r'\s*(?:\d+|-)\s+(?:-?\d+|-|\(\w+\))\s+(.+?)\s*', line)
        require(m is not None, 'evidence', 'Unknown launchd services row')
        labels.append(m[1])
    return labels


def service_query(domain, label):
    target = domain + '/' + label
    p = ctl('print', target)
    if p.returncode:
        require(bool(re.search(r'Could not find service|Could not find specified service|service not found',
                               p.stderr, re.I)), 'evidence', 'Unknown query outcome: ' + target + ': ' + p.stderr)
        return None
    require(p.stdout.startswith(target + ' = {'), 'evidence', 'Returned launchd identity differs: ' + target)
    return p.stdout


def owned_output(text, root):
    for line in text.splitlines():
        # program, working directory, plist path, log path and argument paths.
        value = line.strip().split(' = ', 1)[-1].strip('"')
        if value.startswith('/') and inside(value, root):
            return True
        for token in shlex.split(value) if value and not any(x in value for x in ('{', '}', '"', "'")) else []:
            if token.startswith('/') and inside(token, root):
                return True
    return False


def _ps_pids_now():
    """PIDs visible to the SAME ps source the evidence came from.

    DGN-1638/1639 correction: the first cut re-checked with os.kill, which is a
    DIFFERENT source than the injected MINT_RETIRE_PS probe. In production the
    two agree; under the selftest's stub they cannot, and trusting the OS over
    the injected probe silently deleted the 'bare surviving process refuses'
    guarantee. Re-ask the original source instead -- that fixes the snapshot
    race without changing which authority the verb believes.
    """
    q = run([os.environ.get('MINT_RETIRE_PS', 'ps'), '-axo', 'pid=,ppid=,uid=,command='])
    if q.returncode != 0:
        return None
    out = set()
    for line in q.stdout.splitlines():
        bits = line.split(None, 3)
        if len(bits) == 4 and _signed_int(bits[0]):
            out.add(int(bits[0]))
    return out


def _signed_int(tok):
    return bool(re.fullmatch(r'-?\d+', tok))


# DGN-1783: executable prefixes of OS-owned binaries. All three live on the
# SIP-protected sealed system volume, so neither the agent nor its units can
# place a program there; a process running one of them is the OS's, not ours.
OS_DAEMON_PREFIXES = ('/usr/libexec/', '/System/', '/usr/sbin/')


def _os_binary(path):
    if not path or not path.startswith('/') or os.path.normpath(path) != path:
        return False
    return path.startswith(OS_DAEMON_PREFIXES)


def _os_watcher(row, entries, held):
    """Reason string when a process's ONLY hold on the root is an OS watch.

    DGN-1783: after a WatchPaths unit is booted out, launchd's UserEventAgent
    keeps its fsevents watch open as read-only directory fds on the root.
    Refusing on that forever means a completed agent can never retire on macOS
    unless a system daemon is killed -- which this verb never does. Everything
    here must hold, or the process stays surviving evidence (fail-closed):
      - ps row visible and PPID 1: spawned by launchd itself, so it is not a
        descendant of any agent unit (unit children carry the unit's pid);
      - argv0 (ps) AND the image lsof reports as 'txt' are both an OS binary
        (OS_DAEMON_PREFIXES) -- argv0 alone can be forged with exec -a;
      - every fd on the root is a numbered 'r' DIR fd: no cwd/txt/root entry,
        no regular file, no write or read-write access, no unknown type/mode.
    The command line naming the root is judged separately and still refuses.
    """
    if row is None or row[0] != 1:
        return None
    argv0 = row[2].split(None, 1)[0] if row[2].split() else ''
    txt = [e['path'] for e in entries if e['fd'] == 'txt' and e['path']]
    if not (_os_binary(argv0) and txt and _os_binary(txt[0]) and txt[0] == argv0):
        return None
    if not all(e['fd'].isdigit() and e['access'] == 'r' and e['type'] == 'DIR' for e in held):
        return None
    return ('OS daemon %s (PPID 1, not a descendant of agent units) holds only read-only '
            'directory fds (fsevents/WatchPaths watch); not agent work' % argv0)


def processes(root):
    p = run([os.environ.get('MINT_RETIRE_PS', 'ps'), '-axo', 'pid=,ppid=,uid=,command='])
    require(p.returncode == 0 and not p.stderr, 'evidence', 'Process inspection unavailable')
    rows = {}
    for line in p.stdout.splitlines():
        bits = line.split(None, 3)
        # DGN-1637: uid is signed. macOS runs services as uid -2 (nobody) --
        # contactsd and distnoted agent were two such rows on this machine --
        # and isdigit() rejects the minus, which refused the whole inspection.
        require(len(bits) == 4 and all(_signed_int(x) for x in bits[:3]),
                'evidence', 'Unknown process row')
        rows[int(bits[0])] = (int(bits[1]), int(bits[2]), bits[3])
    excluded, pid = {os.getpid()}, os.getpid()
    while pid in rows and rows[pid][0] not in excluded:
        pid = rows[pid][0]
        excluded.add(pid)
    # Exclude descendants of this process, not siblings of shell ancestors.
    descendants = {os.getpid()}
    for _ in range(len(rows)):
        more = {pid for pid, row in rows.items() if row[0] in descendants}
        if more <= descendants:
            break
        descendants |= more
    excluded |= descendants
    ls = run([os.environ.get('MINT_RETIRE_LSOF', 'lsof'), '-n', '-P', '-u', str(os.getuid()), '-Fpcfatn'])
    require(ls.returncode == 0 and not ls.stderr, 'evidence', 'Open-file/cwd process visibility unavailable')
    seen, pid, files = set(), None, {}
    for line in ls.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:]); seen.add(pid); files.setdefault(pid, [])
        elif line.startswith('f') and pid is not None:
            files[pid].append({'fd': line[1:], 'access': '', 'type': '', 'path': None})
        elif line[:1] in ('a', 't') and pid is not None and files[pid]:
            files[pid][-1]['access' if line[0] == 'a' else 'type'] = line[1:].strip()
        elif line.startswith('n/') and pid is not None:
            if not files[pid] or files[pid][-1]['path'] is not None:
                files[pid].append({'fd': '', 'access': '', 'type': '', 'path': None})
            files[pid][-1]['path'] = line[1:]
    hits, ignored = [], []
    for pid, entries in files.items():
        if pid in excluded:
            continue
        held = [e for e in entries if e['path'] and inside(e['path'], root)]
        if not held:
            continue
        reason = _os_watcher(rows.get(pid), entries, held)
        if reason:
            ignored.append({'pid': pid, 'command': rows[pid][2], 'paths': sorted({e['path'] for e in held}),
                            'reason': reason})
        else:
            hits.extend({'pid': pid, 'path': e['path']} for e in held)
    missing = [pid for pid, row in rows.items() if row[1] == os.getuid() and pid not in excluded and pid not in seen]
    # DGN-1638: the ps snapshot and the lsof snapshot are taken microseconds
    # apart, and short-lived processes die in between. A PID that is GONE has
    # no open files by definition -- treating it as unknown visibility made the
    # verb fail on ordinary machine churn (measured: three pids, all exited
    # before lsof ran). Re-check each one; only a pid that still EXISTS and is
    # still invisible to lsof is genuine unknown visibility.
    ps_now = _ps_pids_now()
    still = missing if ps_now is None else [pid for pid in missing if pid in ps_now]
    require(not still, 'evidence', 'Unknown open-file visibility for PIDs: ' + str(still))
    for pid, row in rows.items():
        if pid not in excluded and re.search(r'(?:^|[\s=])' + re.escape(str(root)) + r'(?=$|[/\s])', row[2]):
            hits.append({'pid': pid, 'command': row[2]})
    # DGN-1639: same snapshot-staleness as the visibility re-check above, on the
    # refusal side. The ps rows were captured earlier in this run; a process that
    # has since EXITED is not surviving evidence, and reporting it tells the
    # owner to stop something that is already gone. Re-confirm each pid exists
    # at refusal time. A pid we cannot signal is kept -- unknown beats absent.
    live_hits = hits if ps_now is None else [h for h in hits if h['pid'] in ps_now]
    require(not live_hits, 'units-up',
            'Surviving process evidence (no automatic kill): ' + json.dumps(live_hits))
    return {'checked_at': now(), 'method': 'ps plus lsof cwd/executable/open files', 'processes': len(seen),
            'ignored': ignored}


def service_mutation_state(r):
    """True description of what this run did to launchd registrations.

    DGN-1640 rider: 'none' was false once an installed plist actually moved
    -- the same class of defect as claiming a killed bridge was untouched.
    Status is read live off r['body']['moves'], so a receipt saved mid-run
    (or mid-restore) reports exactly what has happened so far, not a fixed
    string chosen at plan time.
    """
    installed = [x for x in r['body']['moves'] if x.get('kind') == 'launchagent']
    if not installed:
        return 'none'
    archived = sum(1 for x in installed if x['status'] == 'archived')
    if archived:
        return 'archived %d installed launchd plist(s)' % archived
    restored = sum(1 for x in installed if x['status'] == 'restored')
    if restored:
        return 'restored %d installed launchd plist(s)' % restored
    return 'none'


def down(r, body, stable=False):
    r['service']['availability'] = 'unknown'
    domain = 'gui/' + str(os.getuid())
    loaded = []
    labels = set(r['service']['labels'])
    # Enumerate all registrations, including orphan registrations without plists.
    for dom in (domain, 'user/' + str(os.getuid()), 'system'):
        for label in service_labels(dom):
            text = service_query(dom, label)
            if text is None:
                continue
            if (dom == domain and label in labels) or owned_output(text, r['root']):
                require(dom == domain, 'evidence', 'Unsupported target domain: ' + dom + '/' + label)
                loaded.append(dom + '/' + label)
                labels.add(label)
    for label in sorted(labels):
        if service_query(domain, label) is not None:
            loaded.append(domain + '/' + label)
    if loaded:
        r['service']['availability'] = 'some work has reappeared; availability not confirmed'
        r['service']['loaded'] = sorted(set(loaded))
    require(not loaded, 'units-up', '\n'.join('Loaded: ' + x + '\nlaunchctl bootout ' + shlex.quote(x)
                                            for x in sorted(set(loaded))))
    try:
        proc = processes(Path(r['root']))
        if str(body) != r['root']:
            proc['ignored'] = proc['ignored'] + processes(body)['ignored']
    except Refusal as e:
        if e.gate == 'retire.units-up':
            r['service']['availability'] = 'some work has reappeared; availability not confirmed'
        raise
    before = runtime(body)
    if stable:
        time.sleep(5)
        after = runtime(body)
        require(before == after, 'units-up', 'Continuing runtime writes over five seconds')
        # A fresh launchd query follows the stability interval as well.
        down(r, body, False)
    r['service'].update(availability='stopped and unavailable', checked_at=now(),
                        process_evidence=proc, runtime=before,
                        service_mutations=service_mutation_state(r),
                        queries=[{'domain': domain, 'label': label, 'outcome': 'service-not-found',
                                  'at': now()} for label in sorted(labels)])


def crew_call(fn, *args):
    env = dict(os.environ, PACK_PY3=sys.executable)
    p = subprocess.run(['bash', '-c', '. "$1"; shift; "$@"', 'retire',
                        str(LIB / 'crew_leave.sh'), fn, *args], env=env,
                       capture_output=True, text=True, timeout=60)
    require(p.returncode == 0, 'prepare', 'Crew helper refused: ' + p.stderr + p.stdout)
    return p.stdout


def members(path):
    lines = Path(path).read_text().splitlines()
    require(all(not x or x.startswith(('#', 'member=')) for x in lines), 'evidence', 'Malformed crew registry')
    return [x[7:] for x in lines if x.startswith('member=')]


def pin(path):
    data = Path(path).read_text()
    raw = re.findall(r'^EXPECTED_USER_VERSION\s*=\s*([0-9]+)\s*(?:#.*)?$', data, re.M)
    require(len(raw) == 1, 'restore-incompatible', 'Missing/ambiguous archived DB pin: ' + str(path))
    line = next(x for x in data.splitlines() if re.match(r'^EXPECTED_USER_VERSION\s*=', x))
    return int(raw[0]), line


def db_version(path):
    with sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True) as db:
        return db.execute('PRAGMA user_version').fetchone()[0]


def crew_plan(root, c):
    shared = Path(os.environ.get('SHARED_HOME', os.environ.get('DOGANY_SHARED_HOME', str(Path.home() / '.dogany')))).resolve()
    directory = Path(os.environ.get('CREW_DIR', os.environ.get('DOGANY_CREW_DIR', str(shared / 'crews')))).resolve()
    result = []
    installed = packs(c)
    for registry in sorted(directory.glob('*/crew.conf')):
        rows = members(registry)
        matching = [x for x in rows if Path(x).resolve() == root]
        if not matching:
            continue
        kit = registry.parent.name
        # DGN-1716: a declared crew (crew.declared, no kit installed) has no
        # DB, pin or lease to restore; retire would record nothing to undo.
        require(not (registry.parent / 'crew.declared').exists(), 'evidence',
                'Declared crew membership: %s -- leave it first: '
                'scripts/pack/crew_leave.sh leave --root %s --kit %s' % (kit, root, kit))
        require(len(matching) == len(set(matching)), 'evidence',
                'Duplicate membership cannot be exactly restored by canonical rejoin: ' + kit)
        db = shared / 'crews' / kit / (kit + '.db')
        link = root / 'database' / (kit + '.db')
        require(not inside(db, root), 'evidence', 'Shared DB lies inside target body')
        require(kit in installed and link.is_symlink() and link.resolve() == db.resolve(),
                'evidence', 'Crew DRIFT: ' + kit)
        code = root / 'database' / (kit + '.py')
        version, raw = pin(code)
        result.append({'kit': kit, 'registry': str(registry), 'members': matching,
                       'before_digest': digest(registry), 'db': str(db.resolve()),
                       'db_identity': identity(db), 'schema': db_version(db),
                       'pin': version, 'raw_pin': raw, 'code_digest': digest(code),
                       'installed_version': installed[kit], 'status': 'pending', 'backups': []})
    registered = {x['kit'] for x in result}
    for link in (root / 'database').glob('*.db'):
        if link.is_symlink():
            require(link.stem in registered, 'evidence', 'Shared DB lacks canonical crew membership: ' + str(link))
    return result


def no_update_in_flight():
    """DGN-1786: an /update apply run in flight may restart this agent."""
    held = retire_marker.update_in_flight()
    require(held is None, 'busy', 'An update is applying on this host (lock ' +
            str(retire_marker.apply_lock_path()) + ' held by ' + str(held) +
            '); retry after it has delivered its result')


def settle_marker(root, r):
    """Keep the retire marker only while a retirement is in progress.

    Judged on the receipt as written to disk: a run refused before its first
    effect leaves 'prepared' (or the prior state) there even if it had already
    flipped r['status'] in memory, and that agent is untouched.
    """
    status = None
    if r is not None:
        try:
            status = json.loads((Path(r['archive']) / 'RECEIPT.json').read_text()).get('status')
        except (OSError, ValueError, KeyError, AttributeError):
            # Receipt unwritable after an effect: keep the agent marked.
            status = r.get('status')
    if status in ('retiring', 'partial', 'restoring', 'restore-partial'):
        return
    retire_marker.clear(root, pid=os.getpid())


def leases(r):
    for k in r['crew']:
        require(not exists(Path(k['registry']).parent / '.lease'), 'busy', 'Crew update lease: ' + k['kit'])


def _compatible(r, body):
    leases(r)
    for k in r['crew']:
        code = body / 'database' / (k['kit'] + '.py')
        require(identity(k['db']) == k['db_identity'] and digest(code) == k['code_digest'],
                'restore-incompatible', 'DB identity or archived code changed: ' + k['kit'])
        version, raw = pin(code)
        current = db_version(k['db'])
        require(version == current == k['pin'], 'restore-incompatible',
                f"{k['kit']}: archived pin={version}, current schema={current}; keep archive or prepare compatible rebuild")
        for member in members(k['registry']):
            p = Path(member)
            mc = conf(p / '.instance.conf')
            mv = packs(mc).get(k['kit'])
            mp, mr = pin(p / 'database' / (k['kit'] + '.py'))
            link = p / 'database' / (k['kit'] + '.db')
            require(mv == k['installed_version'] and mp == version and mr == raw and
                    link.is_symlink() and link.resolve() == Path(k['db']),
                    'restore-incompatible', 'Current crew code/version/binding differs: ' + member)


def compatible(r, body):
    try:
        _compatible(r, body)
    except Refusal as e:
        if e.gate in ('retire.busy', 'retire.restore-incompatible'):
            raise
        raise Refusal('restore-incompatible', e.detail) from e
    except (OSError, ValueError, sqlite3.Error) as e:
        raise Refusal('restore-incompatible', 'Compatibility proof unavailable: ' + str(e)) from e


def observer_load(path):
    require(not Path(path).is_symlink(), 'prepare', 'Observer state may not be a symlink')
    state = json.loads(Path(path).read_text()) if exists(path) else {}
    require(isinstance(state, dict) and isinstance(state.get('jobs', {}), dict), 'prepare', 'Invalid observer state')
    state.setdefault('jobs', {})
    return state


def observer_plan(root, labels, tx, why='intentional completed-agent retirement'):
    directory = Path(os.environ.get('DOGANY_HEALTH_OBSERVER_STATE_DIR', str(Path.home() / '.dogany/health-observer'))).resolve()
    require(not inside(directory, root), 'evidence', 'Target contains observer authority')
    path = directory / 'jobs.json'
    state = observer_load(path)
    entries = {}
    for label in labels:
        old = state['jobs'].get(label, {})
        require(isinstance(old, dict), 'prepare', 'Invalid observer job: ' + label)
        entries[label] = {'previous_present': 'retired' in old, 'previous': old.get('retired'),
                          'prior_loss': {k: v for k, v in old.items() if 'notif' in k or 'alert' in k},
                          'marker': {'when': now(), 'why': why,
                                     'transaction_id': tx, 'status': 'committed',
                                     'domain': 'gui/' + str(os.getuid()), 'label': label}}
    return {'path': str(path), 'protocol': 'legacy-unlocked-readback', 'entries': entries,
            'status': 'pending', 'attempts': 0, 'retry_required': False}


def write_markers(r):
    o = r['observer']
    for attempt in range(2):
        state = observer_load(o['path'])
        for label, entry in o['entries'].items():
            state['jobs'].setdefault(label, {})['retired'] = entry['marker']
        atomic(o['path'], state)
        observed = observer_load(o['path'])
        o['attempts'] += 1
        missing = [label for label, entry in o['entries'].items()
                   if observed['jobs'].get(label, {}).get('retired') != entry['marker']]
        o['missing'] = missing
        if not missing:
            o['status'] = 'complete'
            return
        o['retry_required'] = True
    # Owner explicitly accepts continuing after two failed acknowledgments.
    o['status'] = 'partial'


def marker_conflicts(r):
    state = observer_load(r['observer']['path'])
    for label, e in r['observer']['entries'].items():
        actual = state['jobs'].get(label, {}).get('retired')
        # Missing owned marker is the accepted observer-clearing race.
        already = actual == e['previous'] if e['previous_present'] else actual is None
        require(actual is None or actual == e['marker'] or already, 'restore-conflict',
                'Observer marker now belongs to another operation: ' + label)


def restore_markers(r):
    marker_conflicts(r)
    o = r['observer']; state = observer_load(o['path'])
    for label, e in o['entries'].items():
        entry = state['jobs'].setdefault(label, {})
        if e['previous_present']:
            entry['retired'] = e['previous']
        else:
            entry.pop('retired', None)
            for key in ('lost_at', 'lost_notified_at', 'last_notified_at', 'notified_at'):
                entry.pop(key, None)
            entry['first_seen_at'] = now()
            entry['last_seen_at'] = now()
        e['restore_cas'] = 'restored'
    atomic(o['path'], state)
    read = observer_load(o['path'])
    for label, e in o['entries'].items():
        entry = read['jobs'].get(label, {})
        require(('retired' in entry) == e['previous_present'] and
                entry.get('retired') == e['previous'], 'restore-conflict', 'Observer restore readback changed')
    o['status'] = 'restored'


def receipt_digest(r):
    return hashlib.sha256(json.dumps({k: v for k, v in r.items() if k != 'receipt_digest'},
                                     sort_keys=True).encode()).hexdigest()


def save(r):
    r['updated_at'] = now()
    r['receipt_digest'] = receipt_digest(r)
    atomic(Path(r['archive']) / 'RECEIPT.json', r)


def intent(r, site):
    r['intent'] = site
    r['events'].append({'site': site, 'phase': 'intent', 'at': now()})
    save(r)


def observed(r, site):
    r['events'].append({'site': site, 'phase': 'observed', 'at': now()})
    r['intent'] = None
    save(r)


def location(r):
    root, archived = Path(r['root']), Path(r['archive']) / 'instance'
    matches = [p for p in (root, archived) if p.is_dir() and identity(p) == r['generation']['identity']]
    require(len(matches) == 1, 'recovery', 'Cannot reconcile original body identity')
    p = matches[0]
    require(tree_hash(p) == r['body_digest'], 'restore-conflict', 'Original body changed since preparation')
    require(not (p == archived and exists(root)), 'restore-conflict', 'Original slot has a replacement; no old membership/marker changes allowed')
    return p


def rename_exclusive(source, destination):
    # Darwin RENAME_EXCL (stdio.h): atomic no-overwrite, without a lock.
    # The pre-site recheck alone cannot exclude a newly occupied destination.
    libc = ctypes.CDLL(None, use_errno=True)
    require(hasattr(libc, 'renamex_np'), 'prepare', 'Atomic exclusive rename unavailable')
    fn = libc.renamex_np
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(os.fsencode(source), os.fsencode(destination), 0x00000004):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def move_item(item, reverse=False):
    src, dst = Path(item['source']), Path(item['destination'])
    if reverse:
        src, dst = dst, src
    if not exists(src) and exists(dst) and identity(dst) == item['identity']:
        item['status'] = 'restored' if reverse else 'archived'
        return
    require(exists(src) and not exists(dst) and identity(src) == item['identity'],
            'restore-conflict', 'Rename identity/destination conflict: ' + str(src))
    require(tree_hash(src) == item['digest'], 'restore-conflict', 'Moved object changed: ' + str(src))
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(src.stat().st_dev == dst.parent.stat().st_dev, 'prepare', 'Cross-filesystem rename refused')
    rename_exclusive(src, dst)
    syncdir(src.parent); syncdir(dst.parent)
    require(identity(dst) == item['identity'], 'recovery', 'Rename readback failed')
    item['status'] = 'restored' if reverse else 'archived'


def report(r, detail=''):
    completed_sites = [f"{x}: {r[x]['status']}" for x in ('body', 'observer')]
    completed_sites += [f"crew {k['kit']}: {k['status']}" for k in r['crew']]
    if ledger_resolved(r):
        completed_sites.append('minting ledger: ' + r['minting_ledger']['status'])
    else:
        # DGN-1725: never read as done. Old receipts say 'not-supplied'.
        completed_sites.append('minting ledger: UNRESOLVED (no parent record, no --instance-root); '
                               'clear the parent binding with: ' +
                               r.get('minting_ledger', {}).get('clear_command',
                                                               ledger_clear_command(r['name'], r['root'])) +
                               ' -- or resume with --instance-root <parent-instance-root>')
    remaining = [x for x in completed_sites if not x.endswith(('complete', 'restored'))]
    print('Target: ' + r['name'] + ' (' + r['root'] + ')')
    print('Availability: ' + r['service'].get('availability', 'unknown') +
          '; last confirmed stopped at ' + r['service'].get('checked_at', 'never') + '; no service was started')
    for item in r['body']['moves']:
        found = []
        for key, label in (('source', 'original location'), ('destination', 'saved in archive')):
            try:
                if exists(item[key]) and identity(item[key]) == item['identity']:
                    found.append(label + ': ' + item[key])
            except OSError:
                pass
        print('Body object: ' + ('; '.join(found) if found else 'location unknown: ' + item['source']))
    print('Completed/observed: ' + '; '.join(completed_sites))
    print('Remaining: ' + ('; '.join(remaining) or 'none'))
    print('Receipt: ' + str(Path(r['archive']) / 'RECEIPT.json'))
    print('Choices: resume with mint_run.sh retire --resume ' + shlex.quote(str(Path(r['archive']) / 'RECEIPT.json')) +
          '; keep archive and inspect later')
    try:
        body = location(r)
        compatible(r, body); marker_conflicts(r)
        print('Restore choice: mint_run.sh retire --restore ' + shlex.quote(r['archive']) + ' (services stay stopped)')
    except Exception as e:
        print('Restore currently unavailable: ' + str(e))
    print('Observer races remain tolerated; an already queued or later raced alarm cannot be retracted.')
    if detail:
        print('Detail: ' + detail)


PARENT_RECORD = 'config/minting-parent.json'
MINTING_STATE = LIB.parents[2] / 'skills/dogany-mint-agent/scripts/minting_state.py'


def ledger_clear_command(name, root, path=None):
    """The one official clearing call, spelled out for an operator."""
    state = path or '<parent-instance-root>/config/minting-state.json'
    return ' '.join(shlex.quote(x) for x in (
        'python3', str(MINTING_STATE), 'clear')) + ' <domain> --agent ' + shlex.quote(name) + \
        ' --path ' + shlex.quote(str(root)) + ' --state ' + shlex.quote(str(state))


def parent_record(body, name):
    # DGN-1725: minting_state.py accept writes this pointer into the newborn,
    # naming the parent's ledger. It is the minting fact; callers are guesses.
    path = Path(body) / PARENT_RECORD
    if not path.exists() and not path.is_symlink():
        return None
    require(not path.is_symlink(), 'evidence', 'Minting parent record is a symlink')
    try:
        record = json.loads(path.read_text())
        state = record['state']
        ok = (record.get('agent') == name and isinstance(state, str) and
              os.path.isabs(state) and Path(state).name == 'minting-state.json')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        ok = False
    require(ok, 'evidence', 'Minting parent record is invalid: ' + str(path))
    return Path(state)


def ledger_plan(root, name, caller, body=None):
    # The ledger belongs to the minting parent, as in pack_install step 11.
    # DGN-1725: the newborn's own parent record wins; the old ladder
    # (--instance-root > $DOGANY_INSTANCE_ROOT > cwd instance) is only the
    # fallback for newborns minted before the record existed. No estate-wide
    # search: an unresolved ledger is reported, never guessed.
    recorded = parent_record(body or root, name)
    if recorded is not None:
        path, source = recorded, 'parent-record'
        require(not caller or (Path(caller).resolve() / 'config/minting-state.json').resolve()
                == path.resolve(), 'evidence',
                'Minting parent record names another ledger than --instance-root: ' + str(path))
    else:
        source = 'instance-root' if caller else None
        if not caller and os.environ.get('DOGANY_INSTANCE_ROOT'):
            caller, source = os.environ['DOGANY_INSTANCE_ROOT'], 'env'
        if not caller and (Path.cwd() / '.instance.conf').is_file():
            caller, source = str(Path.cwd()), 'cwd'
        if not caller:
            return {'status': 'unresolved', 'entries': [],
                    'clear_command': ledger_clear_command(name, root)}
        path = Path(caller).resolve() / 'config/minting-state.json'
    require(not path.is_symlink(), 'evidence', 'Minting ledger is a symlink')
    path = path.resolve()
    state = json.loads(path.read_text()) if path.exists() else {'domains': {}}
    entries = []
    for domain, row in state['domains'].items():
        if row.get('dedicated_agent') != name:
            continue
        recorded = row.get('agent_path')
        require(not recorded or Path(recorded).resolve() == root,
                'evidence', 'Minting ledger slug points at another path: ' + domain)
        entries.append({'domain': domain, 'previous': row, 'status': 'pending'})
    return {'path': str(path), 'entries': entries, 'status': 'pending', 'source': source}


def ledger_resolved(r):
    return bool((r.get('minting_ledger') or {}).get('path'))


def ledger_backup(r):
    # DGN-1725: one pre-change copy per transaction, beside the ledger;
    # never overwritten, so a resumed retirement keeps the original.
    ledger = r['minting_ledger']
    src = Path(ledger['path'])
    dst = src.with_name(src.name + '.bak-retire-' + r['transaction_id'])
    if not dst.exists():
        fd, tmp = tempfile.mkstemp(prefix='.retire-', dir=src.parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(src.read_bytes())
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, src.stat().st_mode & 0o777)
            os.link(tmp, dst)                      # exclusive: never clobbers a backup
            syncdir(dst.parent)
        finally:
            os.unlink(tmp)
    ledger['backup'] = str(dst)


def ledger_apply(r, restore=False):
    ledger = r.get('minting_ledger')
    if not ledger or not ledger.get('path'):
        return
    script = MINTING_STATE
    for entry in ledger['entries']:
        state = json.loads(Path(ledger['path']).read_text())
        row = state.get('domains', {}).get(entry['domain'], {})
        if restore:
            require(not row.get('dedicated_agent') or
                    (row.get('dedicated_agent') == r['name'] and
                     row.get('agent_path') == r['root']),
                    'restore-conflict', 'Minting ledger has a replacement agent')
            args = ['accept', entry['domain'], '--agent', r['name'], '--path', r['root']]
            if entry['previous'].get('minted_at'):
                args += ['--date', entry['previous']['minted_at']]
        else:
            if not row.get('dedicated_agent'):
                entry['status'] = 'complete'
                continue  # clear completed before a receipt write; resume safely
            require(row == entry['previous'], 'restore-conflict', 'Minting ledger changed since preparation')
            args = ['clear', entry['domain'], '--agent', r['name']]
            if row.get('agent_path'):
                args += ['--path', r['root']]           # slug AND path, or clear refuses
            ledger_backup(r)
        result = run([sys.executable, str(script), *args, '--state', ledger['path']])
        require(result.returncode == 0, 'partial', 'Minting ledger update failed: ' + result.stderr)
        actual = json.loads(Path(ledger['path']).read_text())['domains'][entry['domain']]
        require(actual.get('dedicated_agent') == (r['name'] if restore else None),
                'partial', 'Minting ledger readback failed')
        entry['status'] = 'restored' if restore else 'complete'
        save(r)
    ledger['status'] = 'restored' if restore else 'complete'


def plan(args):
    spelled = Path(args.root).absolute()
    require(not spelled.is_symlink(), 'evidence', 'Symlink root refused')
    root = spelled.resolve()
    c, evidence = completed(root, args.instance_root)
    require(hasattr(ctypes.CDLL(None), 'renamex_np'), 'prepare', 'Atomic exclusive rename unavailable')
    tx = str(uuid.uuid4())
    archive_parent = Path(args.archive_dir).resolve() if args.archive_dir else root.parent / '.retired'
    require(not inside(archive_parent, root), 'prepare', 'Archive must be outside the mint slot')
    nearest = archive_parent
    while not nearest.exists():
        nearest = nearest.parent
    require(nearest.stat().st_dev == root.stat().st_dev, 'prepare', 'Archive must share the body filesystem')
    archive = archive_parent / (c['DOGANY_AGENT_NAME'] + '.' + tx)
    inventory = plist_inventory(root)
    issues = []
    try:
        crew = crew_plan(root, c)
    except Exception as e:
        issues.append(Refusal('evidence', 'Crew inventory: ' + str(e)))
        crew = []
    try:
        observer = observer_plan(root, inventory, tx)
    except Exception as e:
        issues.append(e if isinstance(e, Refusal) else Refusal('prepare', 'Observer state: ' + str(e)))
        observer = {}
    r = {'schema_version': 1, 'transaction_id': tx, 'operation': 'retire', 'status': 'prepared',
         'created_at': now(), 'host': socket.gethostname(), 'uid': os.getuid(),
         'root': str(root), 'spelled_root': str(spelled), 'name': c['DOGANY_AGENT_NAME'],
         'generation': {'identity': identity(root), 'minted_at': c['DOGANY_MINTED_AT'],
                        'config_digest': digest(root / '.instance.conf'), 'evidence': evidence},
         'archive': str(archive), 'body_digest': tree_hash(root),
         'service': {'labels': inventory, 'domain': 'gui/' + str(os.getuid()), 'service_mutations': 'none'},
         'crew': crew, 'body': {'status': 'pending', 'moves': []}, 'events': [], 'intent': None,
         'current_gate': None, 'next_actions': ['resume', 'restore'],
         'observer': observer,
         'minting_ledger': ledger_plan(root, c['DOGANY_AGENT_NAME'], args.instance_root)}
    paths = [(root, archive / 'instance')]
    # DGN-1640 rider: an installed copy in the shared LaunchAgents directory
    # is a live registration, not part of the instance tree, so it survives
    # the root move above and is left as a ghost -- same shape mint_teardown
    # already closes (mint_teardown.sh:442-445). Archive every inventory
    # entry that plist_inventory identified as OURS but that does not live
    # inside root, into archive/LaunchAgents/, matching teardown's layout.
    # DGN-1646: archive the complete encoded prefix family, including engines.
    for transcript_dir in transcript_store.family(str(spelled)):
        paths.append((transcript_dir, archive / 'transcripts' / transcript_dir.name))
    seen_installed = set()
    for entries in inventory.values():
        for entry in entries:
            p = Path(entry['path'])
            if inside(p, root):
                continue
            key = str(p.resolve())
            if key in seen_installed:
                continue
            seen_installed.add(key)
            paths.append((p, archive / 'LaunchAgents' / p.name))
    handoff = Path(os.environ.get('DOGANY_HANDOFF_ROOT', str(Path.home() / '.dogany/handoff'))) / c['DOGANY_AGENT_NAME']
    if exists(handoff):
        paths.append((handoff, archive / 'handoff' / handoff.name))
    if args.instance_root:
        journal = Path(args.instance_root) / '.mint-journal' / (c['DOGANY_AGENT_NAME'] + '.json')
        if exists(journal):
            paths.append((journal, archive / 'mint-journal' / journal.name))
    launchagents_archive = archive / 'LaunchAgents'
    for src, dst in paths:
        require(not src.is_symlink() and src.stat().st_dev == root.stat().st_dev,
                'evidence', 'Unresolved/cross-filesystem sidecar: ' + str(src))
        move = {'source': str(src), 'destination': str(dst),
                'identity': identity(src), 'digest': tree_hash(src),
                'mode': src.stat().st_mode & 0o7777, 'status': 'pending'}
        if dst.parent == launchagents_archive:
            move['kind'] = 'launchagent'
        r['body']['moves'].append(move)
    token = root / '.telegram_bot/.env'
    require(not token.is_symlink(), 'evidence', 'Credential file must be preserved inside the body')
    r['credentials'] = {'policy': 'preserve', 'bot_token_present': token.is_file(),
                        'relative_path': 'instance/.telegram_bot/.env',
                        'mode': (token.stat().st_mode & 0o7777) if token.is_file() else None}
    r['plan_digest'] = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
    for check in (lambda: down(r, root, stable=True), lambda: leases(r), no_update_in_flight):
        try:
            check()
        except Exception as e:
            issues.append(e if isinstance(e, Refusal) else Refusal('evidence', str(e)))
    if issues:
        rank = ['request', 'recovery', 'evidence', 'units-up', 'busy',
                'restore-conflict', 'restore-incompatible', 'prepare', 'partial']
        issues.sort(key=lambda e: (rank.index(e.gate.split('.', 1)[1]), e.detail))
        first = issues[0]
        raise Refusal(first.gate.split('.', 1)[1], first.detail + ''.join(
            '\nSecondary ' + x.gate + ': ' + x.detail for x in issues[1:]))
    return r


def load_receipt(path):
    try:
        path = Path(path).resolve(strict=True)
        r = json.loads(path.read_text())
        require(isinstance(r, dict), 'recovery', 'Receipt is not an object')
        require(r.get('receipt_digest') == receipt_digest(r), 'recovery', 'Receipt content digest mismatch')
        require(r['schema_version'] == 1 and r['status'] in STATES and
                r['host'] == socket.gethostname() and r['uid'] == os.getuid() and
                Path(r['archive']).resolve() == path.parent and path.name == 'RECEIPT.json',
                'recovery', 'Receipt schema/host/UID/archive mismatch')
        for key in ('generation', 'body', 'service', 'crew', 'observer', 'events', 'body_digest',
                    'root', 'name', 'transaction_id', 'operation'):
            require(key in r, 'recovery', 'Incomplete receipt: ' + key)
        require(r['operation'] in ('retire', 'restore') and Path(r['root']).is_absolute() and
                isinstance(r['body']['moves'], list) and isinstance(r['crew'], list) and
                isinstance(r['service']['labels'], dict) and isinstance(r['observer']['entries'], dict),
                'recovery', 'Invalid receipt site structure')
        return r
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise Refusal('recovery', 'Cannot read valid retirement receipt: ' + str(e)) from e


def retire(r):
    body = location(r)
    down(r, body, stable=True)
    leases(r)
    r['operation'] = 'retire'; r['status'] = 'retiring'
    # Ordered three-site best effort: verify -> markers/readback -> body -> crew.
    down(r, body)
    marker_conflicts(r)
    intent(r, 'observer')
    write_markers(r)
    r['observer_markers'] = r['observer']['status']
    observed(r, 'observer')
    down(r, body)
    intent(r, 'body')
    for item in r['body']['moves']:
        down(r, body)
        move_item(item)
        body = Path(r['archive']) / 'instance'
        save(r)
        down(r, body)
    r['body']['status'] = 'complete'
    observed(r, 'body')
    for k in r['crew']:
        down(r, body); leases(r); location(r)
        intent(r, 'crew:' + k['kit'])
        for member in k['members']:
            output = crew_call('_crew_leave_one', k['kit'], member, 'retire-' + r['transaction_id'], '0')
            k['backups'] += [line.split(' ', 2)[2] for line in output.splitlines() if line.startswith('LEFT ')]
        require(not any(x in members(k['registry']) for x in k['members']), 'prepare', 'Crew removal readback failed')
        # Recover backup paths even if a previous leave completed before its journal update.
        registry = Path(k['registry'])
        k['backups'] = sorted(str(p) for p in registry.parent.glob(
            registry.name + '.bak-leave-retire-' + r['transaction_id'] + '-*'))
        k['after_digest'] = digest(k['registry']); k['status'] = 'complete'
        observed(r, 'crew:' + k['kit'])
    if ledger_resolved(r):
        intent(r, 'minting-ledger')
        ledger_apply(r)
        observed(r, 'minting-ledger')
    down(r, body)
    final_state = observer_load(r['observer']['path'])
    r['observer']['final_missing'] = [label for label, e in r['observer']['entries'].items()
        if final_state['jobs'].get(label, {}).get('retired') != e['marker']]
    if r['observer']['final_missing']:
        r['observer']['status'] = 'partial'
        r['observer_markers'] = 'partial'
    require(r['observer']['status'] == 'complete', 'partial', 'Observer markers incomplete after readback; body and crew retired, alarm may arrive')
    # DGN-1725: a parent ledger still bound to this agent makes the next mint
    # of its domain ledger-stale, so an unresolved ledger is not complete.
    require(ledger_resolved(r), 'partial', 'Minting ledger unresolved: the parent instance is unknown, '
            'so its binding to ' + r['name'] + ' may remain; clear it with: ' +
            r.get('minting_ledger', {}).get('clear_command', ledger_clear_command(r['name'], r['root'])))
    r['status'] = 'complete'; r['current_gate'] = None; r['next_actions'] = ['restore']
    save(r)


def restore(r):
    body = location(r)
    down(r, body, stable=True)
    compatible(r, body)
    marker_conflicts(r)
    for item in r['body']['moves']:
        dst, src = Path(item['source']), Path(item['destination'])
        require(not exists(dst) or (not exists(src) and identity(dst) == item['identity']),
                'restore-conflict', 'Restore destination occupied: ' + str(dst))
    r['status'] = 'restoring'; r['operation'] = 'restore'
    for item in r['body']['moves']:
        down(r, body); compatible(r, body)
        intent(r, 'restore-body')
        move_item(item, reverse=True)
        body = Path(r['root'])
        save(r)
        down(r, body)
        observed(r, 'restore-body')
    if r.get('minting_ledger', {}).get('path'):
        intent(r, 'restore-minting-ledger')
        ledger_apply(r, restore=True)
        observed(r, 'restore-minting-ledger')
    r['body']['status'] = 'restored'
    for k in r['crew']:
        down(r, body); compatible(r, body)
        intent(r, 'restore-crew:' + k['kit'])
        for member in k['members']:
            crew_call('_crew_rejoin_upsert', k['kit'], member)
        require(all(members(k['registry']).count(x) == 1 for x in k['members']),
                'restore-conflict', 'Crew restore readback differs')
        k['status'] = 'restored'
        observed(r, 'restore-crew:' + k['kit'])
    down(r, body); compatible(r, body)
    intent(r, 'restore-observer')
    restore_markers(r)
    observed(r, 'restore-observer')
    down(r, body)
    r['status'] = 'restored'; r['current_gate'] = None; r['next_actions'] = []
    save(r)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal('request', message)


def main(argv=None):
    r = None
    entry_events = 0
    marked = None   # DGN-1786: root whose retire marker this run wrote
    try:
        p = Parser(description=__doc__)
        mode = p.add_mutually_exclusive_group(required=True)
        mode.add_argument('--root'); mode.add_argument('--resume'); mode.add_argument('--restore')
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--archive-dir'); p.add_argument('--instance-root')
        args = p.parse_args(argv)
        require(sys.platform == 'darwin', 'request', 'Retirement supports owning-user macOS launchd only')
        require(not (args.archive_dir and not args.root), 'request', '--archive-dir requires --root')
        if args.root:
            spelled = Path(args.root).absolute()
            if not args.dry_run and spelled.is_dir() and not spelled.is_symlink():
                # DGN-1786: mark BEFORE plan() looks for an update in flight.
                # (A root plan() will refuse on its own is never marked.)
                marked = spelled.resolve()
                retire_marker.write(marked, operation='retire', name=marked.name,
                                    receipt=None, started_at=now())
            r = plan(args)
            if args.dry_run:
                print('Dry run: COMPLETED, services down; markers -> archive -> crew leave; no writes')
                ledger = r['minting_ledger']
                print('Minting ledger: ' + ('%s (%s), %d binding(s) to clear' % (
                    ledger['path'], ledger['source'], len(ledger['entries'])) if ledger.get('path')
                    else 'UNRESOLVED -- retirement would end retire.partial; clear with: ' +
                    ledger['clear_command']))
                return 0
            archive = Path(r['archive'])
            missing = []
            parent = archive
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700, exist_ok=False)
                syncdir(directory.parent)
            save(r)
            retire_marker.write(marked, operation='retire', name=r['name'], started_at=now(),
                                transaction_id=r['transaction_id'],
                                receipt=str(Path(r['archive']) / 'RECEIPT.json'))
        else:
            path = Path(args.restore) / 'RECEIPT.json' if args.restore else Path(args.resume)
            r = load_receipt(path)
            entry_events = len(r['events'])
            if not args.dry_run:
                marked = Path(r['root'])
                retire_marker.write(marked, operation=r['operation'], name=r['name'],
                                    started_at=now(), transaction_id=r['transaction_id'],
                                    receipt=str(Path(r['archive']) / 'RECEIPT.json'))
                no_update_in_flight()
            body = location(r)
            down(r, body, stable=True)
            if r['operation'] == 'retire' and not args.restore and not ledger_resolved(r) \
                    and not args.dry_run:
                # DGN-1725: a resume may now name the parent (or the body may
                # carry the record, if it was added since preparation).
                ledger = ledger_plan(Path(r['root']), r['name'], args.instance_root, body=body)
                if ledger.get('path'):
                    r['minting_ledger'] = ledger
                    save(r)
                    if r['status'] == 'complete':         # pre-DGN-1725 receipt, never checked
                        intent(r, 'minting-ledger')
                        ledger_apply(r)
                        observed(r, 'minting-ledger')
            if args.restore or r['operation'] == 'restore':
                compatible(r, body); marker_conflicts(r)
            if args.dry_run:
                if 'partial' in r['status']:
                    print('retire.partial')
                report(r, 'Read-only recovery inspection')
                return 6 if 'partial' in r['status'] else 0
        if r['status'] == 'restored':
            report(r, 'Already restored; current services verified down')
            return 0
        if args.restore or r['operation'] == 'restore':
            restore(r)
        elif r['status'] == 'complete':
            report(r, 'Already retired; current services verified down')
            return 0
        else:
            retire(r)
        report(r)
        return 0
    except Exception as e:
        gate = e.gate if isinstance(e, Refusal) else 'retire.prepare'
        detail = e.detail if isinstance(e, Refusal) else type(e).__name__ + ': ' + str(e)
        effected = r is not None and (r.get('intent') or len(r.get('events', [])) > entry_events
                                      or 'partial' in r.get('status', ''))
        if effected:
            r['status'] = 'restore-partial' if r['operation'] == 'restore' else 'partial'
            r['current_gate'] = 'retire.partial'; r['cause'] = {'gate': gate, 'detail': detail}
            r['next_actions'] = ['resume', 'restore', 'inspect']
            try:
                if not args.dry_run:
                    save(r)
            except Exception as write_error:
                detail += '; RECEIPT WRITE FAILED: ' + str(write_error)
            print('retire.partial')
            report(r, detail)
            return 6
        print(gate)
        print('Detail: ' + detail)
        return 2 if gate == 'retire.request' else (1 if gate == 'retire.prepare' else 3)
    finally:
        if marked is not None:
            settle_marker(marked, r)


if __name__ == '__main__':
    sys.exit(main())
