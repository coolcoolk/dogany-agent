#!/usr/bin/env python3
"""Read-only view of an agent's known external resources and residuals.

Uses the delete planner's resource readers, without lifecycle eligibility
checks or service mutations. This is an inventory, not a deletion approval
or a disk-usage estimate. Missing ownership declarations remain unmeasured.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
from pathlib import Path

import agent_residual
import mint_retire


def inspect(args):
    root = Path(os.path.abspath(args.root))
    if root != root.resolve() or not root.is_dir():
        raise ValueError('root must be a physical instance directory')
    marker = root / '.instance.conf'
    if marker.is_symlink() or not marker.is_file():
        raise ValueError('root must contain a physical .instance.conf')
    if not root.stat().st_mode & 0o444 or not root.stat().st_mode & 0o111:
        raise ValueError('instance directory is unreadable')
    conf = agent_residual.lenient_conf(marker)
    if not conf.get('DOGANY_AGENT_NAME'):
        raise ValueError('.instance.conf has no DOGANY_AGENT_NAME')
    peers, discovery_error = agent_residual.discovered_roots()
    res = agent_residual.residual(root, conf, peers, args.managed_targets, args.roster or ())
    box = agent_residual.mailbox(root, conf)
    estate = mint_retire.estate_plan(root)
    current, other = agent_residual.receipts_for_root(
        root, root.parent / '.retired', conf.get('DOGANY_MINTED_AT'))
    errors = [discovery_error] if discovery_error else []
    errors += [x['error'] for x in res['peer_confs_unreadable']]
    errors += [x['error'] for x in estate['discovery']['peers_unreadable']]
    for db in res['db']:
        if db['status'] != 'measured' or any(t['rows'] is None for t in db['tables']):
            errors.append('DB %s: %s (some ownership rows unmeasured)' % (db['kit'], db['status']))
    for doc in [res['managed_targets']] + res['rosters']:
        if doc.get('path') and doc['status'] != 'read':
            errors.append('%s: %s' % (doc['path'], doc['status']))
    return {'schema': 'agent.footprint/1', 'root': str(root),
            'name': conf['DOGANY_AGENT_NAME'], 'slug': agent_residual.slug_of(root, conf),
            'generation': {'identity': agent_residual.identity(root),
                           'minted_at': conf.get('DOGANY_MINTED_AT')},
            'mailbox': box, 'estate': estate, 'residual': res,
            'receipts': {'current': current, 'other_generations': other},
            'scope': 'known delete resources; no service-state or full writer-coverage verdict',
            'errors': errors, 'rc': 5 if errors else 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--managed-targets')
    parser.add_argument('--roster', action='append')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = inspect(args)
    except (OSError, ValueError, RuntimeError, mint_retire.Refusal) as error:
        result = {'schema': 'agent.footprint/1', 'root': args.root, 'rc': 5, 'errors': [str(error)]}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('Footprint: ' + result['root'])
        if 'mailbox' in result:
            box = result['mailbox']
            print('Mailbox: %s (inbox %d, tmp %d)' % (box['path'], box['inbox'], box['tmp']))
            print('Estate: ' + mint_retire.estate_summary(result['estate']))
            for line in agent_residual.residual_lines(result['residual']):
                print(line)
            print('Scope: ' + result['scope'])
        for error in result['errors']:
            print('Unknown: ' + error)
    return result['rc']


if __name__ == '__main__':
    sys.exit(main())
