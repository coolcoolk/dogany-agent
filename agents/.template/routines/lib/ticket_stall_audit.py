"""Read-only ticket/run reconciliation; inbox writes are owned by the caller.

DGN-2164: a failed run, missed start, or newer owner ask must return to an
agent turn. This module never changes a ticket and never sends a notification.

DGN-2189: the audit is devkit-lane only. The framework wires it nowhere by
default; both injection entry points (periodic, session_start) return nothing
unless the devkit is recorded as installed in the instance pack registry
(.instance.conf DOGANY_PACKS). Findings are scoped (actionable items always;
"no run" only for tickets opened 2-72 hours ago), ordered actionable first
then newest no-run tickets, and capped including one count line.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time

TICKET = re.compile(r'\b[A-Z][A-Z0-9]*-\d+\b')
FAILED = {'error', 'canceled', 'cancelled', 'expired', 'timeout', 'incomplete'}
CLOSED = {'done', 'dismissed', 'backlog'}
DEVKIT = 'kit_dev'  # catalog id (packs/catalog.json, kind=kit)
CAP = 20
ULID = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


def devkit_installed(workspace):
    """True iff the instance pack registry records the devkit."""
    try:
        with open(Path(workspace) / '.instance.conf', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if line.startswith('DOGANY_PACKS='):
                    packs = line.split('=', 1)[1].strip().strip('\'"')
                    return any(x.strip().split('@', 1)[0] == DEVKIT for x in packs.split(','))
    except OSError:
        pass
    return False


def stamp(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            parsed = dt.datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
            return parsed.timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def fields(text):
    if not text.startswith('---\n'):
        return {}
    front = text.split('---', 2)[1]
    return {k: v.strip().strip('\'"') for k, v in
            re.findall(r'^([\w.-]+):[ \t]*([^\n]*)', front, re.M)}


def records(workspace, shared):
    out = {}
    dispatch = workspace / '.telegram_bot' / 'dispatch'
    for path in dispatch.glob('*/status.json'):
        doc = read_json(path)
        ticket = doc.get('ticket')
        if not ticket:
            continue
        at = stamp(doc.get('started_at')) or path.stat().st_mtime
        out.setdefault(ticket, []).append((at, doc.get('result') or 'running', doc.get('runid') or path.parent.name))
    for kind in ('queue', 'closed', 'leases'):
        for path in (shared / 'dispatcher' / kind).glob('*.json'):
            doc = read_json(path)
            source = doc.get('ticket_doc') or doc
            ticket = source.get('product_ticket')
            if not ticket:  # Rows written before product_ticket was persisted.
                argv = source.get('argv') or []
                if '--ticket' in argv and argv.index('--ticket') + 1 < len(argv):
                    ticket = argv[argv.index('--ticket') + 1]
            if ticket:
                at = stamp(doc.get('granted_wall') or source.get('submitted_wall')) or path.stat().st_mtime
                result = doc.get('closed') or ('running' if kind == 'leases' else 'queued')
                out.setdefault(ticket, []).append((at, result, doc.get('runid') or doc.get('id', path.stem)))
    return out


DATE = re.compile(r'20\d\d-\d\d-\d\d(?:[ T]\d\d:\d\d)?')
DECISION = re.compile(r'\bdec-\d+\b', re.I)
OWNER_ASK = re.compile(
    r'\[owner-ask\]|\b(?:waits? (?:for )?|pending |needs? )owner '
    r'(?:copy )?confirm(?:ation)?\b', re.I)
OWNER_ANSWER = re.compile(r'\[owner-answer\]|\bowner copy (?:confirmed|approved)\b|'
                          r'\bcopy confirm(?:ation)? (?:received|recorded)\b', re.I)
ANSWERED = re.compile(r'\b(?:answered|confirmed|decided|closed|resolved|dismissed)\b|'
                      r'\uD655\uC815|\uC644\uB8CC', re.I)
PENDING = re.compile(r'\b(?:open|pending|unanswered|waiting)\b|\uB300\uAE30|\uBBF8\uACB0', re.I)


def decision_state(workspace, decision, cache):
    """Read the decision's current status, never infer an ask from its owner."""
    if decision not in cache:
        try:
            text = (workspace / 'worklog/decisions' / (decision + '.md')).read_text(encoding='utf-8')
        except OSError:
            text = ''
        statuses = re.findall(r'^\s*(?:-\s*)?status:\s*(.*)', text, re.I | re.M)
        status = fields(text).get('status') or (statuses[-1] if statuses else '')
        # A recorded answer overrides a stale "pending" title/status.
        answers = re.findall(r'^\s*(?:-\s*)?(?:#{1,3}\s*)?'
                             r'(?:answer|decision|\uACB0\uC815|\uB2F5\uBCC0)'
                             r'(?:[ \t]*\([^\n)]*\))?[ \t]*:[ \t]*([^\n]*)', text, re.I | re.M)
        answer = any(a.strip() and a.strip().lower() not in {'null', 'none', 'tbd', '-'}
                     and not PENDING.search(a) for a in answers)
        if ANSWERED.search(status) or answer:
            cache[decision] = 'answered'
        elif PENDING.search(status or text.split('\n', 1)[0]):
            cache[decision] = 'pending'
        else:
            cache[decision] = 'unknown'
    return cache[decision]


def owner_ask_time(workspace, text, front, cache):
    """Require an unanswered decision or an explicit pending owner-ask signal.

    Transcript ticket mentions and dated owner quotes are not questions.
    Only body lines count; frontmatter owner/source/refs describe provenance.
    """
    if ANSWERED.search(front.get('owner_ask_status', '')):
        return 0
    ask = stamp(front.get('owner_ask_at'))
    answered = 0
    parts = text.split('---', 2)
    body = parts[2] if text.startswith('---\n') and len(parts) == 3 else text
    for line in body.splitlines():
        # Scheduling context does not identify the decision being asked.
        line = re.sub(r'\([^)]*\b(?:after|before) dec-\d+[^)]*\)', '', line, flags=re.I)
        date = DATE.search(line)
        at = stamp(date.group()) if date else stamp(front.get('updated_at') or front.get('updated'))
        if OWNER_ANSWER.search(line):
            answered = max(answered, at)
            continue
        states = [decision_state(workspace, d.lower(), cache) for d in DECISION.findall(line)]
        explicit = OWNER_ASK.search(line)
        if 'pending' not in states and not (explicit and 'answered' not in states):
            continue
        ask = max(ask, at)
    return ask if ask > answered else 0


def ulid_time(value):
    """Millisecond timestamp carried in a ULID's first 10 characters."""
    value = str(value or '').upper()
    if len(value) != 26 or any(c not in ULID for c in value):
        return 0
    at = 0
    for c in value[:10]:
        at = at * 32 + ULID.index(c)
    return at / 1000.0


def created_time(workspace, path, value, now, ulid=None):
    """A bare date is not midnight: refine it before the 2h rule (DGN-2189)."""
    created = stamp(value)
    if not re.fullmatch(r'20\d\d-\d\d-\d\d', str(value)):
        return created or ulid_time(ulid) or path.stat().st_mtime
    if now - created >= 2 * 86400:
        return created  # Old enough that the hour cannot change the verdict.
    precise = ulid_time(ulid) or getattr(path.stat(), 'st_birthtime', 0)
    if not precise:
        try:
            result = subprocess.run(['git', '-C', str(workspace), 'log', '-1', '--diff-filter=A',
                                     '--format=%ct', '--', str(path.relative_to(workspace))],
                                    capture_output=True, text=True, timeout=5)
            precise = float(result.stdout.strip()) if result.stdout.strip() else 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            precise = 0
    return max(created, precise or path.stat().st_mtime)


def backlog_intent(f):
    """An open ticket filed as backlog is never a stall."""
    marks = ' '.join(f.get(k, '') for k in ('intent', 'lane', 'horizon', 'tags', 'labels')).lower()
    return 'backlog' in marks or f.get('backlog', '').lower() in {'true', 'yes', '1'}


def park_time(workspace, path):
    """Recover the actual park transition when an owner ask updated the file."""
    relative = str(path.relative_to(workspace))
    try:
        log = subprocess.run(['git', '-C', str(workspace), 'log', '-20', '--format=%ct %H', '--', relative],
                             capture_output=True, text=True, timeout=5)
        parked = 0
        for line in log.stdout.splitlines():
            at, rev = line.split()
            old = subprocess.run(['git', '-C', str(workspace), 'show', rev + ':' + relative],
                                 capture_output=True, text=True, timeout=5)
            if fields(old.stdout).get('status') != 'parked':
                break
            parked = float(at)
        return parked
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def findings(workspace, shared=None, now=None, cap=CAP):
    workspace = Path(workspace)
    shared = Path(shared or os.environ.get('DOGANY_SHARED_HOME') or Path.home() / '.dogany')
    now = time.time() if now is None else now
    runs = records(workspace, shared)
    decisions = {}
    found = {}
    for path in sorted((workspace / 'worklog').rglob('*.md')):
        try:
            text = path.read_text(encoding='utf-8')
            f = fields(text)
            ticket = f.get('id') or (TICKET.search(path.name).group() if TICKET.search(path.name) else '')
            if not TICKET.fullmatch(ticket) or f.get('status') not in {'open', 'wip', 'blocked', 'parked'}:
                continue
            if backlog_intent(f):
                continue
            history = sorted(runs.get(ticket, []))
            created = created_time(workspace, path, f.get('created_at') or f.get('created'), now, f.get('ulid'))
            touched = stamp(f.get('updated_at') or f.get('updated')) or path.stat().st_mtime
            latest = history[-1] if history else None
            priority = (f.get('priority') or '').upper()
            reason, actionable = '', True
            if f.get('status') == 'parked':
                parked = stamp(f.get('parked_at') or f.get('parked') or f.get('updated'))
                ask = owner_ask_time(workspace, text, f, decisions)
                if ask and not (f.get('parked_at') or f.get('parked')):
                    parked = park_time(workspace, path) or parked
                if parked and ask > parked:
                    reason = 'owner ask newer than park; unpark or state the blocking decision'
            elif latest and latest[1] in FAILED and max(touched, latest[0]) >= now - 72 * 3600:
                reason = 'latest run %s ended %s; no follow-up run' % (latest[2], latest[1])
            elif not history and now - 72 * 3600 <= created <= now - 2 * 3600:
                reason, actionable = 'opened >2h ago; no run', False
            if reason:
                rank = int(priority[1:]) if re.fullmatch(r'P\d', priority) else 9
                found[ticket] = ((not actionable, rank if actionable else 0,
                                  created if actionable else -created, ticket),
                                 '[ticket-stall] %s: %s. Next turn: act or say why not.' % (ticket, reason))
        except OSError:
            continue
    ordered = [line for _, line in sorted(found.values())]
    if cap and len(ordered) > cap:
        ordered = ordered[:cap - 1] + ['[ticket-stall] +%d more (see ticket list)' % (len(ordered) - cap + 1)]
    return ordered


def session_start(data):
    """SessionStart payload for a devkit hook; None when nothing to inject."""
    if os.environ.get('DOGANY_DISPATCH_WORKER') or data.get('agent_id'):
        return None
    workspace = data.get('cwd') or os.getcwd()
    if not devkit_installed(workspace):
        return None
    lines = findings(workspace)
    if not lines:
        return None
    return {'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                   'additionalContext': '\n'.join(lines)}}


def periodic(workspace, shared=None, now=None):
    """Called by the 5-minute health tick; audit every 30 minutes (devkit only).

    Atomic inbox replacement coalesces overdue findings until consumption.
    Failed writes leave the clock unchanged so the next tick retries.
    """
    workspace = Path(workspace)
    now = time.time() if now is None else now
    if not devkit_installed(workspace):
        stale = workspace / '.telegram_bot' / 'session-inbox' / 'ticket-stall-audit.md'
        if stale.exists():
            stale.unlink()  # Written before DGN-2189; never inject it on a non-devkit install.
        return []
    state = workspace / '.telegram_bot' / 'ticket-stall-audit.json'
    if now - float(read_json(state).get('at', 0)) < 1800:
        return []
    lines = findings(workspace, shared, now)
    inbox = workspace / '.telegram_bot' / 'session-inbox'
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / 'ticket-stall-audit.md'
    if lines:
        tmp = inbox / ('.ticket-stall-audit.%s.tmp' % os.getpid())
        tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        tmp.replace(dest)
    elif dest.exists():
        dest.unlink()  # Don't inject a resolved finding that was never consumed.
    temp = state.with_suffix('.%s.tmp' % os.getpid())
    temp.write_text(json.dumps({'at': now}), encoding='utf-8')
    temp.replace(state)
    return lines
