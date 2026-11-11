#!/usr/bin/env python3
"""crew_lead.py -- DGN-2040: the Python half of scripts/pack/crew_lead.sh.

Kept in a file, not inlined: bash 3.2 (macOS /bin/bash) mis-parses a heredoc
inside $(...) once it holds quotes or parentheses.

Subcommands (all print one result on stdout, rc 0 unless the call is wrong):
  packs-kits <conf> <catalog>        kit ids recorded in DOGANY_PACKS, one per line
  manifest-field <manifest> <field>  a top-level string field ('' if absent)
  db-derived <manifest>              1 when capabilities.db_derived is true, else 0
  resolve <seat> <shared-skills> -- <argv...>
                                     'OK <script-index> <path>' or 'REFUSE <why>'
  replay <ctx> <receipts> <kit> <fresh 0|1> -- <argv...>
                                     JSON {request_id, key, hit}
  receipt <receipts> <stdout-file> <kit> <seat> <rc> <replay-json> -- <argv...>
  actor-role <ACTORS.md> <name>      the kit_dev ACTORS.md role of <name>
"""
import datetime
import hashlib
import json
import os
import sys


def _after_dashes(args):
    if '--' in args:
        i = args.index('--')
        return args[:i], args[i + 1:]
    return args, []


def packs_kits(conf, catalog):
    """Catalog kind=kit rows in DOGANY_PACKS; an id with no catalog row
    counts when the root also has database/<id>.db (an unknown kit is not
    missed). Last DOGANY_PACKS line wins, like the installer's reader."""
    try:
        rows = {r.get('id'): r for r in json.load(open(catalog)).get('packs', [])}
    except (OSError, ValueError):
        rows = {}
    root = os.path.dirname(conf)
    ids = []
    try:
        for ln in open(conf, encoding='utf-8'):
            if ln.startswith('DOGANY_PACKS='):
                ids = [e.split('@')[0].strip()
                       for e in ln.strip().split('=', 1)[1].split(',') if e.strip()]
    except OSError:
        return []
    out = []
    for i in ids:
        r = rows.get(i)
        if (r and r.get('kind') == 'kit') or os.path.lexists(os.path.join(root, 'database', i + '.db')):
            out.append((r or {}).get('provides_kit') or i)
    return out


def _manifest(path):
    try:
        d = json.load(open(path, encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _under(p, base):
    return p == base or p.startswith(base.rstrip('/') + '/')


def resolve(seat, shared, argv):
    """Section 3.3 step 2 (N11): argv[0] (or the script behind python3 /
    bash) must be a file under the seat's database/ or
    .claude/skills-bundle/. Interpreter flags (-c, -m, ...) are refused.
    A share-mode skill body may live in the crew's own shared-skills
    canonical (DGN-956); every other real path must stay in the seat."""
    if not argv:
        return 'REFUSE no command'
    rseat = os.path.realpath(seat)
    rshared = os.path.realpath(shared)
    interp = os.path.basename(argv[0])
    idx = 0
    if interp in ('python3', 'python', 'bash', 'sh'):
        if len(argv) < 2:
            return "REFUSE '%s' with no script" % interp
        if argv[1].startswith('-'):
            return ("REFUSE '%s %s': interpreter flags are refused (no -c / -m / inline code); "
                    "name a script inside the seat" % (interp, argv[1]))
        idx = 1
    cmd = argv[idx]
    lex = os.path.normpath(cmd if os.path.isabs(cmd) else os.path.join(seat, cmd))
    allowed = [os.path.join(seat, 'database'), os.path.join(seat, '.claude', 'skills-bundle')]
    if not any(_under(lex, a) for a in allowed):
        return ("REFUSE '%s' is not under the seat's database/ or .claude/skills-bundle/ -- "
                "the runner executes the kit's own commands only" % cmd)
    real = os.path.realpath(lex)
    if not (_under(real, rseat) or _under(real, rshared)):
        return "REFUSE '%s' resolves outside the seat (%s)" % (cmd, real)
    base = os.path.basename(real)
    if base.endswith('.db') or '.db-' in base:
        return "REFUSE '%s' is a database file -- no raw ledger access" % cmd
    if not os.path.isfile(real):
        return "REFUSE '%s' is not a file in the seat" % cmd
    return 'OK %d %s' % (idx, lex)


def _request_id(ctx):
    try:
        c = json.load(open(ctx, encoding='utf-8'))
    except (OSError, ValueError):
        return None
    rid = c.get('request_id') if isinstance(c, dict) else None
    return str(rid) if rid else None


def replay(ctx, receipts, kit, fresh, argv):
    """Section 3.5: key = sha256(request_id NUL kit NUL argv...), only in an
    owner turn. A recorded rc=0 under the same key is a hit unless --fresh."""
    rid = _request_id(ctx)
    key = hashlib.sha256('\0'.join([rid, kit] + argv).encode()).hexdigest() if rid else ''
    hit = None
    if key and fresh != '1':
        try:
            for ln in open(receipts, encoding='utf-8'):
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get('key') == key and r.get('rc') == 0:
                    hit = r
        except OSError:
            pass
    return {'request_id': rid, 'key': key, 'hit': hit}


def receipt(receipts, outf, kit, seat, rc, rp, argv):
    data = open(outf, 'rb').read()
    rec = {
        'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'kit': kit, 'argv': argv, 'cwd': seat,
        'request_id': rp.get('request_id'), 'key': rp.get('key') or None,
        'rc': rc, 'stdout_sha256': hashlib.sha256(data).hexdigest(),
        'stdout': data[:65536].decode('utf-8', 'replace'),
    }
    with open(receipts, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


def actor_role(path, name):
    """kit_dev R&R source: the $CREW/ACTORS.md table (id, type, role)."""
    name = name.lower()
    try:
        lines = open(path, encoding='utf-8').read().splitlines()
    except OSError:
        return '<no ACTORS.md>'
    cols = None
    for ln in lines:
        if not ln.strip().startswith('|'):
            continue
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        low = [c.lower() for c in cells]
        if cols is None and 'id' in low and 'role' in low:
            cols = (low.index('id'), low.index('role'))
            continue
        if cols and len(cells) > max(cols) and cells[cols[0]].lower() == name:
            return cells[cols[1]] or '<empty>'
    return '<no row>'


def main(args):
    if not args:
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == 'packs-kits':
        for k in packs_kits(rest[0], rest[1]):
            print(k)
    elif cmd == 'manifest-field':
        v = _manifest(rest[0]).get(rest[1])
        print(v if isinstance(v, str) else '')
    elif cmd == 'db-derived':
        caps = _manifest(rest[0]).get('capabilities') or {}
        print(1 if isinstance(caps, dict) and caps.get('db_derived') is True else 0)
    elif cmd == 'resolve':
        head, argv = _after_dashes(rest)
        print(resolve(head[0], head[1], argv))
    elif cmd == 'replay':
        head, argv = _after_dashes(rest)
        print(json.dumps(replay(head[0], head[1], head[2], head[3], argv)))
    elif cmd == 'replay-hit':
        h = json.loads(rest[0]).get('hit')
        if not h:
            return 1
        sys.stdout.write(h.get('stdout', ''))
        sys.stderr.write('crew_lead: replay %s\n' % h.get('ts', ''))
    elif cmd == 'receipt':
        head, argv = _after_dashes(rest)
        receipt(head[0], head[1], head[2], head[3], int(head[4]), json.loads(head[5]), argv)
    elif cmd == 'actor-role':
        print(actor_role(rest[0], rest[1]))
    elif cmd == 'realpath':
        print(os.path.realpath(rest[0]))
    else:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
