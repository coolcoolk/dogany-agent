#!/usr/bin/env python3
"""Report disagreements between estate records and resources, without repairs.

Registry and optional MANAGED-TARGETS catalog declare instance roots. Physical
instances declare their main service label and immutable mailbox SLUG. Crew
membership uses the existing crew predicate. Installed LaunchAgents are read
from disk; this is not a verdict on running processes or loaded services.
Exit 0 = clean, 10 = drift, 5 = incomplete scan, 2 = usage.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex

import agent_list
import agent_residual
import mint_retire
import crew_lead


def command(*args):
    return ' '.join(shlex.quote(str(a)) for a in args)


def build(managed=None):
    rows, sources, errors = [], [], []

    def drift(source, subject, says, other_source, other_says, repair):
        rows.append({'source': source, 'subject': str(subject), 'says': says,
                     'other_source': other_source, 'other_says': other_says, 'repair': repair})

    def read(source, path, reader):
        try:
            value = reader()
            sources.append({'source': source, 'path': str(path), 'status': 'ok'})
            return value
        except (OSError, ValueError, RuntimeError, mint_retire.Refusal) as error:
            errors.append('%s %s: %s' % (source, path, error))
            sources.append({'source': source, 'path': str(path), 'status': 'unknown'})
            return None

    registry = agent_list.home() / '.dogany/instances'
    records = {}
    lines = read('registry', registry, lambda: registry.read_text().splitlines() if registry.exists() else [])
    for line in lines or []:
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) != 2 or not os.path.isabs(cells[1]):
            errors.append('registry malformed row: ' + line)
            continue
        root = os.path.realpath(cells[1])
        if root in records:
            drift('registry', root, 'duplicate rows', 'instance', 'one root', command('vi', registry))
        records[root] = cells[0]
    catalog = read('MANAGED-TARGETS', managed, lambda: agent_list.managed_targets(managed)) if managed else None
    found, _, error = agent_list.discover()
    sources.append({'source': 'instances', 'status': 'unknown' if error else 'ok'})
    if error:
        errors.append(error)
    members, confs, crew_errors = agent_list.crews()
    errors.extend(crew_errors)
    sources.extend({'source': 'crew.conf', 'path': p, 'status': 'ok'} for p in confs)
    roots = {os.path.realpath(p) for _, p in found} | set(records) | set(catalog or {}) | set(members)
    instances = {}
    labels, slugs, declared_labels, service_plists = {}, {}, {}, {}
    for directory in agent_list.scan_dirs():
        read('instance-scan', directory, lambda: list(directory.iterdir()))
    for raw in sorted(roots):
        root = Path(raw)
        status = agent_list.probe_root(root)
        if status == 'absent':
            if raw in records:
                drift('registry', raw, 'registered', 'instance', 'absent', command('vi', registry))
            if catalog is not None and raw in catalog:
                drift('MANAGED-TARGETS', raw, 'cataloged', 'instance', 'absent', command('vi', managed))
            continue
        if status != 'instance':
            errors.append('%s: %s' % (raw, status))
            continue
        conf = read('instance', root / '.instance.conf', lambda: agent_list.read_conf(root / '.instance.conf'))
        if conf is None:
            continue
        instances[raw] = conf
        if raw not in records:
            drift('registry', raw, 'absent', 'instance', 'present', command('bash', 'install.sh', '--root', root))
        elif conf.get('DOGANY_AGENT_CLASS') and records[raw] != conf['DOGANY_AGENT_CLASS']:
            drift('registry', raw, records[raw], 'instance', conf['DOGANY_AGENT_CLASS'], command('vi', registry))
        if catalog is not None and raw not in catalog:
            drift('MANAGED-TARGETS', raw, 'absent', 'instance', 'present', command('vi', managed))
        slug = read('SLUG', root / 'config/agent.conf', lambda: agent_residual.slug_of(root, conf))
        if slug:
            if slug in ('.', '..') or '/' in slug or any(c.isspace() for c in slug):
                errors.append('invalid SLUG at ' + raw)
            else:
                slugs.setdefault(slug, []).append(raw)
        for folder in ('bridge', 'routines'):
            for path in sorted((root / folder).glob('*.plist')):
                data = read('instance-label', path, lambda: plistlib.loads(path.read_bytes()))
                if isinstance(data, dict) and isinstance(data.get('Label'), str):
                    declared_labels.setdefault(data['Label'], []).append(raw)
        label_info = read('instance-label', root / 'bridge', lambda: agent_list.launch_host.resolve_label(raw))
        host = read('instance-host', root, lambda: agent_list.launch_host.resolve_host(raw))
        if label_info and label_info[0] and host and host['host'] != 'app':
            labels.setdefault(label_info[0], []).append(raw)
            service_plists[raw] = label_info[1]
    for slug, owners in sorted(slugs.items()):
        if len(owners) > 1:
            drift('SLUG', slug, owners, 'mailbox', 'one recipient required', command('vi', Path(owners[-1]) / 'config/agent.conf'))

    launch = Path(os.environ.get('DOGANY_LAUNCH_AGENTS_DIR', str(agent_list.home() / 'Library/LaunchAgents')))
    plists = read('LaunchAgents', launch, lambda: sorted(p for p in launch.iterdir() if p.suffix == '.plist') if launch.exists() else [])
    installed = {}
    for path in plists or []:
        data = read('LaunchAgents', path, lambda: plistlib.loads(path.read_bytes()))
        if not isinstance(data, dict):
            continue
        label = data.get('Label', '')
        vals = [data.get('WorkingDirectory'), data.get('Program')] + (data.get('ProgramArguments') or [])
        owners = {r for r in instances if any(isinstance(v, str) and os.path.isabs(v) and
                   (v == r or v.startswith(r + '/')) for v in vals)}
        if not isinstance(label, str) or not label:
            errors.append('LaunchAgents missing Label: ' + str(path))
            continue
        if not owners and label not in labels and not label.startswith('com.telegram-skill-bot.'):
            continue
        installed.setdefault(label, []).append(path)
        expected = declared_labels.get(label, labels.get(label, []))
        args = data.get('ProgramArguments') or []
        explicit = [os.path.realpath(args[i + 1]) for i, arg in enumerate(args[:-1])
                    if arg == '--path' and isinstance(args[i + 1], str)]
        if explicit:
            owners = set(explicit)
        if expected and owners and not owners.intersection(expected):
            drift('LaunchAgents', path, sorted(owners), 'instance-label', expected, command('vi', path))
        elif label not in declared_labels:
            drift('LaunchAgents', path, label, 'instance-label', 'no matching declaration', command('vi', path))
    for label, owners in sorted(labels.items()):
        if len(owners) > 1:
            drift('instance-label', label, owners, 'LaunchAgents', 'label must be unique', command('vi', Path(owners[-1]) / '.instance.conf'))
        if label not in installed:
            # Copy only; starting/reloading is a separate operator decision.
            source = service_plists.get(owners[0])
            repair = (command('mkdir', '-p', launch) + ' && ' + command('cp', source, str(launch) + '/')
                      if source else command('vi', Path(owners[0]) / 'bridge' / (label + '.newbridge.plist')))
            drift('LaunchAgents', label, 'absent', 'instance-label', owners, repair)
        elif len(installed[label]) > 1:
            drift('LaunchAgents', label, [str(p) for p in installed[label]], 'instance-label', 'one label', command('vi', installed[label][-1]))

    handoff = Path(os.environ.get('DOGANY_HANDOFF_ROOT', str(agent_list.shared_home() / 'handoff')))
    boxes = read('mailboxes', handoff, lambda: sorted(p for p in handoff.iterdir() if p.is_dir() and not p.name.startswith('.')) if handoff.exists() else [])
    for slug, owners in sorted(slugs.items()):
        missing = [sub for sub in ('inbox', 'done', 'dead', 'tmp') if not (handoff / slug / sub).is_dir()]
        if missing:
            drift('mailboxes', handoff / slug, {'missing': missing}, 'SLUG', owners,
                  command('bash', Path(owners[0]) / 'routines/handoff-provision.sh'))
    for box in boxes or []:
        if box.name not in slugs:
            drift('mailboxes', box, 'present', 'SLUG', 'no recipient',
                  command('mv', box, handoff.parent / ('orphaned-handoff-' + box.name)))

    for raw, kits in sorted(members.items()):
        for kit in sorted(set(kits)):
            repair = command('bash', 'scripts/pack/crew_leave.sh', 'leave', '--root', raw, '--kit', kit)
            verdict = read('crew.conf', raw + ':' + kit, lambda: mint_retire.crew_call('_crew_member_verdict', kit, raw).strip())
            if verdict and verdict not in ('OK', 'DECLARED'):
                if verdict.startswith('DRIFT'):
                    repair = command('bash', 'scripts/pack/pack_install.sh', instances.get(raw, {}).get('DOGANY_AGENT_NAME', Path(raw).name), raw, '--pack', kit)
                drift('crew.conf', raw, {'kit': kit, 'membership': 'registered'}, 'physical-membership', verdict, repair)
            if kits.count(kit) > 1:
                drift('crew.conf', raw, {'kit': kit, 'rows': kits.count(kit)}, 'instance', 'one membership', command('vi', agent_list.crew_home() / kit / 'crew.conf'))
    for raw in sorted(instances):
        kits = crew_lead.packs_kits(str(Path(raw) / '.instance.conf'), str(Path(__file__).resolve().parents[3] / 'packs/catalog.json'))
        for kit in kits:
            if kit not in members.get(raw, []):
                drift('crew.conf', raw, {'kit': kit, 'membership': 'absent'}, 'instance', 'installed kit',
                      command('bash', 'scripts/pack/update_seed.sh', '--target', raw, '--kit', kit))
    return {'schema': 'agent.reconcile/1', 'rows': rows, 'drift': len(rows), 'errors': errors,
            'scanned_sources': sources, 'rc': 5 if errors else (10 if rows else 0)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--managed-targets')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    result = build(args.managed_targets)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('Reconcile: %d drift row(s)' % result['drift'])
        for row in result['rows']:
            print('%s: %s says %s; %s says %s; repair: %s' % (
                row['subject'], row['source'], row['says'], row['other_source'], row['other_says'], row['repair']))
        for error in result['errors']:
            print('Unknown: ' + error)
    return result['rc']


if __name__ == '__main__':
    sys.exit(main())
