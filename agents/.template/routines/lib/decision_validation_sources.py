"""Read-only source observations; missing lifecycle evidence never means done."""
import collections
import json
from pathlib import Path
import sqlite3


def native(ident, at, kinds, states, **extra):
    return dict(id=ident, at=at, kinds=kinds, states=states,
                outcome_known=False, **extra)


def terminal_records(raw, until, parse):
    # Collect identities before interpreting event kind or surface. Sorting
    # dated events prevents an older retry result from closing a newer run.
    grouped, defects = collections.defaultdict(list), []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            ident = event['id']
            if not isinstance(ident, str) or not ident.strip():
                raise ValueError('invalid event identity')
        except (ValueError, TypeError, KeyError):
            defects.append({'line': number, 'problem': 'unreadable-run-row'})
            continue
        try:
            instant = parse(event.get('at'))
        except (ValueError, AttributeError, TypeError):
            instant = None
        grouped[ident].append((instant, number, event))
    records = []
    for ident, events in sorted(grouped.items()):
        dated = sorted((e for e in events if e[0] is not None), key=lambda e: (e[0], e[1]))
        past = [e for e in dated if e[0] < until]
        unknown = [e for e in events if e[0] is None]
        at = dated[0][2]['at'] if dated else None
        record = native(ident, at, ['execution'], ['uncollected'], runs=[], active=True, created_at=None,
                        observed_at=[e[2].get('at') for e in events], issues=[],
                        last_observed_at=past[-1][2]['at'] if past else None,
                        current_state=dated[-1][2].get('state', dated[-1][2].get('event')) if dated else None)
        live = None

        def problem(number, reason):
            defects.append({'line': number, 'id': ident, 'problem': reason})
            record['issues'].append(reason)

        for instant, number, event in past + unknown:
            kind, at = event.get('event'), event.get('at')
            record['outcome_at'] = None
            record['active'] = True
            if instant is None:
                problem(number, 'run-time-unknown')
                continue
            if kind not in ('beat', 'heartbeat'):
                record['last_transition_at'] = at
            if kind in ('open', 'start', 'retry'):
                if live is not None:
                    problem(number, 'unfinished-retry')
                if event.get('surface') != 'dispatch':
                    problem(number, 'unknown-run-surface')
                    live = None
                    continue
                if record['created_at'] is None:
                    record['created_at'] = at
                live = {'id': ident, 'status': 'running', 'receipt': None, 'started_at': at}
                record['runs'].append(live)
            elif kind in ('close', 'expired', 'resolve'):
                if live is None:
                    problem(number, 'missing-start-receipt')
                    continue
                state = 'failed' if kind == 'expired' else event.get('state')
                if state not in ('done', 'failed', 'cancelled'):
                    problem(number, 'run-outcome-unknown')
                    continue
                live.update(status=state, receipt=event.get('evidence'), closed_at=at)
                if state == 'failed':
                    record['states'].append('failed-run')
                if state == 'cancelled':
                    record['states'].append('cancelled')
                live = None
                record.update(active=False, outcome_at=at)
            elif kind in ('beat', 'heartbeat'):
                if live is None:
                    problem(number, 'missing-start-receipt')
            else:
                problem(number, 'unreadable-run-row')
        record['outcome_known'] = bool(record.get('outcome_at')) and not record['issues']
        if (past or unknown) and not record['outcome_known'] and not record['issues']:
            record['issues'].append('run-outcome-uncollected')
        records.append(record)
    return records, defects


def kit_records(path, until, parse):
    """Read overlays and action-only judgments through a read-only connection."""
    records = []
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT t.*,o.state FROM ticket t '
                            'LEFT JOIN decision_overlay o ON o.ticket_id=t.id '
                            'WHERE o.ticket_id IS NOT NULL OR EXISTS '
                            '(SELECT 1 FROM decision_action a WHERE a.target=t.id)').fetchall()
        for row in rows:
            ident = row['human_label'] or row['ulid']
            actions = conn.execute('SELECT action,status,decided_at FROM decision_action '
                                   'WHERE target=? ORDER BY decided_at,act_id', (row['id'],)).fetchall()
            states, issues, observed = {'uncollected'}, [], [row['created_at'], row['updated_at']]
            kinds, outcome_at, last_at = ['question'], None, None
            try:
                current_in_window = parse(row['updated_at']) < until
            except (IndexError, ValueError, TypeError, AttributeError):
                current_in_window = False
            if current_in_window:
                last_at = row['updated_at']
                if row['state'] == 'confirmed':
                    states.add('answered')
                    outcome_at = last_at
                elif row['state'] == 'pending':
                    states.add('unanswered')
                if row['status'] == 'rejected':
                    states.add('rejected')
                if row['status'] == 'parked':
                    states.add('held')
            else:
                issues.append('kit-state-at-cutoff-unknown')
            for action in actions:
                at = action['decided_at']
                observed.append(at)
                try:
                    if parse(at) >= until:
                        continue
                except (ValueError, TypeError, AttributeError):
                    issues.append('kit-action-time-unknown')
                    continue
                last_at = max(filter(None, (last_at, at)), key=parse)
                if 'execution' not in kinds:
                    kinds.append('execution')
                states.add({'approve': 'allowed', 'hold': 'held', 'reject': 'rejected'}.get(
                    action['action'], 'uncollected'))
                if action['status'].startswith('failed:'):
                    states.add('failed-run')
                elif action['status'] != 'applied':
                    states.add('not-executed')
                # Mutable action status has no completion timestamp. decided_at
                # proves the decision, not when its execution became applied.
            active = bool(states & {'unanswered', 'held', 'not-executed'})
            record = native(ident, row['created_at'], kinds, sorted(states),
                            active=active or not outcome_at or bool(issues), created_at=row['created_at'],
                            outcome_at=outcome_at, last_observed_at=last_at, last_transition_at=last_at,
                            observed_at=observed, issues=issues,
                            current_state={'overlay': row['state'], 'ticket': row['status']})
            record['outcome_known'] = bool(outcome_at) and not active and not issues
            records.append(record)
    return records
