#!/usr/bin/env python3
"""DGN-1482 P1b: what a delete leaves behind or must stop on, read-only.

Shared by mint_retire.py (retire plan additions, recorded in the receipt) and
agent_delete.py (the delete router, both routes). Design source: the DGN-1482
grill, findings 3, 5, 6, 7. Nothing here writes anything.

  discovered_roots   every instance root _discover_instances names (finding 5:
                     retire's estate plan read only the registry and the
                     body's HANDOFF_PEER_MAIN, so a peer with no registry row
                     was invisible)
  mailbox            the handoff mailbox keyed by the stamped SLUG (mint
                     creates it under SLUG, finding 6), with inbox/ and tmp/
                     pending counts
  receipts_for_root  retire receipts naming this root, split by generation
                     (finding 7: one root path has carried several
                     generations; only a receipt of the CURRENT generation is
                     honoured)
  residual           what no delete step removes and the operator must decide:
                     shared-DB rows from pack-declared ownership columns
                     (finding 3: counted, never touched), the MANAGED-TARGETS
                     row and crew roster lines (dev-crew sot docs: framework
                     never writes them, only reports), and peer display keys
                     (RETRO_HEALTH_PEER_NAME, HANDOFF_PEER_<X>, ...) other
                     instances still carry.

Ownership declaration (finding 3): a kit names the columns that tie a shared-DB
row to one agent in config/packs/<kit>.agent_ownership.json inside the
instance, shaped like the memory-engine ledger registry scope:
  {"tables": {"<table>": {"axis": "<column>"}}}
Rows count when lower(<column>) equals the agent's name or SLUG. No
declaration = "undeclared" (unmeasured), never a count of 0.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

LIB = Path(__file__).resolve().parent
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
PEER_KEY = re.compile(r'[A-Z0-9_]*PEER[A-Z0-9_]*')
RETIRE_DONE = ('complete', 'restored')


def lenient_conf(path):
    """KEY=value lines of a conf file; {} when absent; raises OSError when unreadable."""
    out = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(errors='replace').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            out.setdefault(k.strip(), v.strip())
    return out


def same_root(text, root):
    text = (text or '').strip()
    if not text or not os.path.isabs(text):
        return False
    try:
        return text == str(root) or Path(text).resolve() == Path(root).resolve()
    except (OSError, RuntimeError):
        return False


def identity(path):
    s = Path(path).stat()
    return [s.st_dev, s.st_ino]


# ------------------------------------------------------------- discovery

def discovered_roots():
    """(roots [physical path], error or None) from _discover_instances."""
    import agent_list
    found, _cands, err = agent_list.discover()
    return [os.path.realpath(root) for _name, root in found], err


# ------------------------------------------------------------- names

def persona_name(root):
    """'- Name: **X**' of the persona file (persona_resolver.sh order), or None."""
    for rel in ('identity/hot.custom.agent.md', 'PROFILE.md', 'AGENT.md'):
        p = Path(root) / rel
        if p.is_file():
            try:
                m = re.search(r'^\s*-\s*Name:\s*\*{0,2}([^*\n]+?)\*{0,2}\s*$',
                              p.read_text(errors='replace'), re.M)
            except OSError:
                return None
            return m[1].strip() if m else None
    return None


def agent_names(root, instance_conf):
    """Lower-cased names other records may use for this agent, plus the persona name."""
    try:
        own = lenient_conf(Path(root) / 'config/agent.conf')
    except OSError:
        own = {}
    names = {x.strip().lower() for x in (instance_conf.get('DOGANY_AGENT_NAME'), own.get('SLUG'),
                                         Path(root).name) if x and x.strip()}
    return names, persona_name(root)


def slug_of(root, instance_conf):
    try:
        slug = lenient_conf(Path(root) / 'config/agent.conf').get('SLUG', '').strip()
    except OSError:
        slug = ''
    return slug or instance_conf.get('DOGANY_AGENT_NAME', '').strip() or Path(root).name


# ------------------------------------------------------------- mailbox

def handoff_root():
    return Path(os.environ.get('DOGANY_HANDOFF_ROOT', str(Path.home() / '.dogany/handoff')))


def _pending(d):
    try:
        return sum(1 for x in os.listdir(d) if not x.startswith('.'))
    except FileNotFoundError:
        return 0


def mailbox(root, instance_conf):
    """The mailbox mint provisioned (keyed by SLUG), its pending counts.

    A mailbox under the old DOGANY_AGENT_NAME key is used only when the SLUG
    one is absent (pre-SLUG instances; identical on a case-insensitive disk).
    """
    slug = slug_of(root, instance_conf)
    keys = [slug] + [k for k in (instance_conf.get('DOGANY_AGENT_NAME', '').strip(),) if k and k != slug]
    base = handoff_root()
    for key in keys:
        path = base / key
        if os.path.lexists(path):
            return {'path': str(path), 'key': key, 'keyed_by': 'SLUG' if key == slug else 'DOGANY_AGENT_NAME',
                    'inbox': _pending(path / 'inbox'), 'tmp': _pending(path / 'tmp')}
    return {'path': str(base / slug), 'key': slug, 'keyed_by': 'SLUG', 'absent': True,
            'inbox': 0, 'tmp': 0}


def mail_pending(box):
    return box.get('inbox', 0) + box.get('tmp', 0)


# ------------------------------------------------------------- receipts

def receipts_for_root(root, archive_parent, minted_at):
    """Non-complete retire receipts that name ROOT, split by generation.

    'current': the receipt's generation is the one at ROOT now (or the root is
    gone and the receipt's archive holds that generation) -- this is the run
    to resume. 'other': a receipt of an earlier generation of the same path;
    never honoured for this one.
    """
    root = Path(root)
    try:
        here = identity(root) if root.is_dir() else None
    except OSError:
        here = None
    current, other = [], []
    try:
        dirs = sorted(Path(archive_parent).iterdir())
    except OSError:
        return current, other
    for d in dirs:
        receipt = d / 'RECEIPT.json'
        if not receipt.is_file():
            continue
        try:
            r = json.loads(receipt.read_text())
            if not same_root(r.get('root'), root) and r.get('root') != str(root):
                continue
            status = r.get('status')
        except (OSError, ValueError, AttributeError):
            continue
        if status in RETIRE_DONE:
            continue
        gen = r.get('generation') or {}
        entry = {'receipt': str(receipt), 'status': status, 'minted_at': gen.get('minted_at'),
                 'identity': gen.get('identity')}
        same = here is not None and list(gen.get('identity') or []) == here and \
            gen.get('minted_at') == minted_at
        (current if same else other).append(entry)
    return current, other


# ------------------------------------------------------------- residual

def _ownership(root, kit):
    p = Path(root) / 'config/packs' / (kit + '.agent_ownership.json')
    if not p.is_file():
        return None, None
    try:
        d = json.loads(p.read_text())
        tables = d['tables']
        assert isinstance(tables, dict)
        out = {}
        for table, spec in tables.items():
            column = (spec or {}).get('axis')
            if not (IDENT.fullmatch(table) and isinstance(column, str) and IDENT.fullmatch(column)):
                return None, 'malformed declaration entry: %r' % table
            out[table] = column
        return out, None
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as e:
        return None, 'unreadable declaration %s: %s' % (p, e)


def _shared_dbs(root, instance_conf):
    """[(kit, db path)] for kits whose DB is a link out of the body."""
    out = []
    kits = [x.split('@', 1)[0] for x in filter(None, instance_conf.get('DOGANY_PACKS', '').split(','))]
    for kit in kits:
        link = Path(root) / 'database' / (kit + '.db')
        if not link.is_symlink():
            continue
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            out.append((kit, None))
            continue
        try:
            target.relative_to(Path(root).resolve())
            continue                                  # travels with the body
        except ValueError:
            out.append((kit, target))
    return out


def db_residual(root, instance_conf, names):
    rows = []
    for kit, db in _shared_dbs(root, instance_conf):
        entry = {'kit': kit, 'db': str(db) if db else None, 'tables': [], 'touched': False}
        if db is None:
            entry['status'] = 'link dangling'
            rows.append(entry)
            continue
        decl, err = _ownership(root, kit)
        if err:
            entry['status'] = 'unknown: ' + err
        elif decl is None:
            entry['status'] = 'undeclared: kit declares no agent ownership column; not measured'
        else:
            try:
                conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
            except sqlite3.Error as e:
                entry['status'] = 'unknown: ' + str(e)
                rows.append(entry)
                continue
            try:
                for table, column in sorted(decl.items()):
                    cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)]
                    if column not in cols:
                        entry['tables'].append({'table': table, 'column': column,
                                                'rows': None, 'note': 'declared column absent'})
                        continue
                    marks = ','.join('?' * len(names))
                    n = conn.execute('SELECT count(*) FROM "%s" WHERE lower("%s") IN (%s)'
                                     % (table, column, marks), sorted(names)).fetchone()[0]
                    entry['tables'].append({'table': table, 'column': column, 'rows': n})
                entry['status'] = 'measured'
            except sqlite3.Error as e:
                entry['status'] = 'unknown: ' + str(e)
            finally:
                conn.close()
        rows.append(entry)
    return rows


def _name_match(value, names, persona):
    v = (value or '').strip()
    if not v:
        return None
    if v.lower() in names:
        return 'name'
    if persona and len(v) >= 2 and v in persona:
        return 'persona-name'
    return None


def peer_keys(root, names, persona, peers):
    """Peer config keys other than BRIEF_PEERS (retire removes that one) naming this agent."""
    hits, unreadable = [], []
    for other in peers:
        if same_root(other, root):
            continue
        conf = Path(other) / 'config/agent.conf'
        try:
            c = lenient_conf(conf)
        except OSError as e:
            unreadable.append({'conf': str(conf), 'error': e.strerror or str(e)})
            continue
        for key, value in sorted(c.items()):
            if key == 'BRIEF_PEERS' or not PEER_KEY.fullmatch(key):
                continue
            how = 'root' if same_root(value, root) else _name_match(value, names, persona)
            if how:
                hits.append({'conf': str(conf), 'key': key, 'value': value, 'match': how})
    return hits, unreadable


def _doc_lines(path, root, names):
    lines = []
    for n, line in enumerate(Path(path).read_text(errors='replace').splitlines(), 1):
        low = line.lower()
        paths = re.findall(r'`(/[^`]+)`', line)
        if any(same_root(p, root) for p in paths) or str(root) in line or \
                any(re.search(r'(?<![a-z0-9_-])' + re.escape(x) + r'(?![a-z0-9_-])', low) for x in names):
            lines.append({'line': n, 'text': line.strip()[:200]})
    return lines


def sot_rows(path, root, names, kind):
    """MANAGED-TARGETS / roster rows naming the agent (reported; never written)."""
    if not path:
        return {'kind': kind, 'status': 'not checked (no path given)', 'rows': []}
    try:
        return {'kind': kind, 'path': str(path), 'status': 'read', 'rows': _doc_lines(path, root, names)}
    except OSError as e:
        return {'kind': kind, 'path': str(path), 'status': 'unreadable: ' + (e.strerror or str(e)),
                'rows': []}


def residual(root, instance_conf, peers, managed_targets=None, rosters=()):
    names, persona = agent_names(root, instance_conf)
    keys, unreadable = peer_keys(root, names, persona, peers)
    return {'names': sorted(names), 'persona_name': persona,
            'db': db_residual(root, instance_conf, names),
            'peer_keys': keys, 'peer_confs_unreadable': unreadable,
            'managed_targets': sot_rows(managed_targets, root, names, 'MANAGED-TARGETS'),
            'rosters': [sot_rows(p, root, names, 'crew roster') for p in rosters],
            'policy': 'reported only: delete never edits shared-DB rows, dev-crew sot docs '
                      'or other agents\' display keys'}


def residual_lines(res):
    """Operator lines for a residual block (dry-run and receipt report)."""
    out = []
    for d in res.get('db', []):
        if d.get('status') == 'measured':
            out.append('Residual DB %s (%s): %s -- rows kept, never touched' % (
                d['kit'], d['db'], ', '.join('%s.%s=%s' % (t['table'], t['column'],
                                                           t['rows'] if t['rows'] is not None else t.get('note'))
                                             for t in d['tables']) or 'no tables declared'))
        else:
            out.append('Residual DB %s (%s): %s' % (d['kit'], d.get('db'), d.get('status')))
    for k in res.get('peer_keys', []):
        out.append('Residual peer key: %s %s=%s (%s match) -- left for the owner' % (
            k['conf'], k['key'], k['value'], k['match']))
    for u in res.get('peer_confs_unreadable', []):
        out.append('Residual unknown: peer config unreadable: %s (%s)' % (u['conf'], u['error']))
    for doc in [res.get('managed_targets')] + list(res.get('rosters', [])):
        if not doc:
            continue
        if doc['status'] != 'read':
            out.append('Residual %s: %s' % (doc['kind'], doc['status']))
        for row in doc['rows']:
            out.append('Residual %s row: %s:%d %s (sot doc: not edited here)' % (
                doc['kind'], doc['path'], row['line'], row['text']))
    return out


if __name__ == '__main__':
    # Read-only inspection aid: agent_residual.py ROOT
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(LIB))
    r = Path(sys.argv[1]).resolve()
    c = lenient_conf(r / '.instance.conf')
    peers, _ = discovered_roots()
    print(json.dumps(residual(r, c, peers), ensure_ascii=False, indent=2))
