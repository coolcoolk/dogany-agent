"""Durable status and return publication for local jobs."""
import hashlib
import json
import os
from pathlib import Path
import time

import return_route as routes


def status(run, values, result, exit_code, notify):
    doc = {k.lower(): v for k, v in values.items()}
    doc.update(result=result, exit_code=int(exit_code), notify=notify,
               ended_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), out_log=str(run / 'out.log'))
    for key in ('model_observed', 'script_usage'):
        value = doc.get(key, '')
        try:
            doc[key] = json.loads(value) if value else ([] if key == 'model_observed' else None)
        except ValueError:
            doc[key] = None
    routes.atomic_json(str(run / 'status.json'), doc)


def publish(workspace, run, values, result, exit_code, route_json, reservation, inbox):
    runid = values['RUNID']
    report = run / 'out.log'
    pinned = routes.validate_route(json.loads(route_json)) if route_json else None
    event = None
    if pinned:
        event = routes.event_for(pinned, 'return:%s:%s' % (pinned['instance_id'], runid),
            runid=runid, ticket=values.get('TICKET') or None, lane=values.get('RUNTIME'),
            result=result, exit_code=int(exit_code), report_path=str(report),
            report_sha256=hashlib.sha256(report.read_bytes()).hexdigest() if report.is_file() else None,
            evidence_refs=[str(report)], requested_notify=values.get('REQUESTED_NOTIFY'))
        routes.persist_event(str(run), pinned, event)
        if pinned['kind'] != 'inbox':
            routes.publish_event(str(run), pinned)
            return
        inbox = pinned['destination']
    lines = ['[dispatch-return] %s: %s' % (result, values.get('DISPLAY_NAME') or values.get('LABEL') or runid),
             '- run: ' + runid, '- runtime: ' + values.get('RUNTIME', ''),
             '- result: %s (exit=%s)' % (result, exit_code), '- out.log: ' + str(report),
             'Agent instruction: read out.log, verify the result, and continue the requested work.']
    if result == 'incomplete':
        lines.append('The worker ended without a final report; recover the unfinished work before claiming completion.')
    if values.get('PLAN_STATE'):
        lines.append('- plan: ' + values['PLAN_STATE'])
    if values.get('SCRIPT_JOB') == '1':
        lines.append('- usage: ' + values.get('SCRIPT_USAGE_FILE', ''))
    with routes.publisher_barriers(workspace, pinned, legacy_inbox=inbox):
        target = Path(inbox)
        target.mkdir(parents=True, exist_ok=True)
        temp = target / ('.dispatch-%s.tmp' % runid)
        temp.write_text('\n'.join(lines) + '\n', encoding='utf-8', errors='replace')
        temp.replace(target / ('dispatch-%s.md' % runid))
        if event:
            receipt = dict(event_id=event['event_id'], payload_sha256=routes.sha256(routes.canonical(event)),
                           destination=inbox, published_at=routes.timestamp())
            routes.atomic_json(str(run / 'return.receipt.json'), receipt)
            routes.atomic_json(str(run / 'return.publication.json'), dict(receipt, state='published'))
        routes.release_reservation(workspace, reservation)


def main():
    import sys
    action, run, result, exit_code, notify = sys.argv[1:]
    values = {k[4:]: v for k, v in os.environ.items() if k.startswith('JOB_')}
    if action == 'status':
        status(Path(run), values, result, exit_code, notify)
    else:
        publish(values['WORKSPACE'], Path(run), values, result, exit_code,
                values.get('RETURN_ROUTE_JSON', ''), values.get('RETURN_RESERVATION', ''), values['SESSION_INBOX_DIR'])


if __name__ == '__main__':
    main()
