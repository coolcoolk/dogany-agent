#!/usr/bin/env python3
"""Rename an agent's display identity; mailbox SLUG and runtime IDs stay fixed.

Dry-run (the default) prints a content-bound transaction ID. Apply recomputes
that plan and requires --confirm ID. Existing identity readers pick up the
name on their next render; this command never restarts a bridge.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

import agent_list
import agent_residual
import mint_retire

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'agents/.template/routines/lib'))
import agent_name


def name_key(name):
    return unicodedata.normalize('NFKC', name).casefold()


def estate(managed=None):
    found, candidates, error = agent_list.discover()
    if error:
        raise ValueError(error)
    members, _, errors = agent_list.crews()
    if errors:
        raise ValueError('; '.join(errors))
    roots = {os.path.realpath(p) for _, p in found} | set(members)
    if managed:
        roots.update(agent_list.managed_targets(managed))
    for p in candidates:
        status = agent_list.probe_root(p)
        if status not in ('instance', 'absent'):
            raise ValueError('%s: %s' % (p, status))
    return sorted(roots)


def identity_file(root):
    for rel in agent_name.IDENTITY_FILES:
        path = root / rel
        if path.is_file():
            if path.is_symlink() or path.resolve() != path:
                raise ValueError('identity must be a physical file: ' + str(path))
            return path
    raise ValueError('instance has no identity file: ' + str(root))


def plan(args):
    root = Path(os.path.abspath(args.root))
    if root != root.resolve() or not root.is_dir():
        raise ValueError('root must be a physical instance directory')
    if (root / '.instance.conf').is_symlink():
        raise ValueError('.instance.conf must be a physical file')
    conf = agent_list.read_conf(root / '.instance.conf')
    if not conf.get('DOGANY_AGENT_NAME'):
        raise ValueError('instance has no DOGANY_AGENT_NAME')
    name = args.name
    if (name != name.strip() or not name or len(name) > 120 or
            any(unicodedata.category(c).startswith('C') for c in name) or
            any(c in name for c in '*`[]<>') or agent_name._unset(name)):
        raise ValueError('display name must be plain text (1-120 characters)')
    path = identity_file(root)
    before = path.read_text()
    pattern = re.compile(r'^- Name:[^\n]*$', re.M)
    if len(pattern.findall(before)) != 1:
        raise ValueError('identity must have exactly one - Name: line')
    after = pattern.sub(lambda _: '- Name: **' + name + '**', before)
    peers = []
    for peer in sorted(set(estate(args.managed_targets)) | {str(root)}):
        p = Path(peer)
        if not (p / '.instance.conf').is_file():
            continue
        peer_path = identity_file(p)
        data = peer_path.read_bytes()
        display = agent_name.display_name(str(p))
        if p != root and name_key(display) == name_key(name):
            raise ValueError('display name collides with another agent: ' + peer)
        peers.append([peer, display, hashlib.sha256(data).hexdigest()])
    binding = {'root': str(root), 'identity': str(path), 'before': before, 'after': after,
               'generation': agent_residual.identity(root), 'conf': conf,
               'slug': agent_residual.slug_of(root, conf), 'peers': peers}
    tx = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:32]
    return path, after, {'schema': 'agent.rename/1', 'mode': 'apply' if args.apply else 'dry-run',
                        'root': str(root), 'identity': str(path), 'slug': binding['slug'],
                        'old_name': agent_name.display_name(str(root)), 'new_name': name,
                        'transaction_id': tx, 'changed': before != after, 'rc': 0, 'errors': []}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--name', '--display-name', required=True)
    parser.add_argument('--managed-targets')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm', help='transaction ID printed by the dry-run')
    parser.add_argument('--json', action='store_true', help='JSON output (also the default)')
    args = parser.parse_args(argv)
    try:
        path, after, result = plan(args)
        if args.apply:
            # Serialize CLI renames across the same estate without a new state
            # file. Lock the existing HOME directory for this estate.
            held = os.open(agent_list.home(), os.O_RDONLY)
            try:
                fcntl.flock(held, fcntl.LOCK_EX)
                path, after, result = plan(args)
                if args.confirm != result['transaction_id']:
                    raise ValueError('apply requires --confirm ' + result['transaction_id'])
                if result['changed']:
                    mint_retire.atomic_text(path, after)
            finally:
                os.close(held)
    except (OSError, ValueError, RuntimeError, mint_retire.Refusal) as error:
        result = {'schema': 'agent.rename/1', 'rc': 2, 'errors': [str(error)]}
    print(json.dumps(result, indent=2))
    return result['rc']


if __name__ == '__main__':
    sys.exit(main())
