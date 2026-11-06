#!/usr/bin/env python3
"""DGN-2172: one member's derived-kit join + pack migration + legacy lane.

No peer install or service operation is performed here. update_apply owns the
member stop/restart boundary. A failed write keeps a durable forward-recovery
marker and the applier keeps the member down; rerunning the same row resumes.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pack_migrate as m


def kit_install(args, root, source, env, dry=False):
    command = ['bash', m.INSTALLER, args.slug, root, '--pack', args.kit,
               '--pack-dir', source, '--source-tag', args.join_tag,
               '--catalog', args.catalog, '--no-state', '--no-start']
    m.run(*command, *(['--dry-run'] if dry else []), env=dict(env, DOGANY_KIT_ISOLATE_DATABASE=args.kit))


def migration(args, root, dry=False):
    return argparse.Namespace(slug=args.slug, root=str(root), old=args.old, new=args.new,
                              catalog=args.catalog, catalog_explicit=True,
                              source_tag=args.source_tag, untagged_local=None, dry_run=dry)


def preserve_local_skills(args, root):
    """Keep a local override only when the vendor body is unchanged.

    A three-way conflict still refuses. This preserves the agent's release-note
    additions through the established local layer without editing skill bytes.
    """
    version = m.mounted(root, args.old)
    if not version:
        return
    old_repo, new_repo = m.coordinate(args.catalog, args.old), m.coordinate(args.catalog, args.new)
    old_tag = 'pack/%s/v%s' % (args.old, version)
    old_manifest = m.git_manifest(old_repo, old_tag)
    new_manifest = m.git_manifest(new_repo, args.source_tag)
    names = {s['name'] if isinstance(s, dict) else s for s in new_manifest.get('skills', [])}
    record = root / ('config/packs/' + args.old + '.skills')
    additions = []
    for name in record.read_text().splitlines() if record.exists() else []:
        if name not in names or not m.TOKEN.fullmatch(name):
            continue
        live = root / '.claude/skills-bundle' / name / 'SKILL.md'
        if not live.is_file():
            continue
        path = '/skills-bundle/' + name + '/SKILL.md'
        original = m.run('git', '-C', old_repo, 'show', old_tag + ':' + old_manifest.get('payload_root', 'payload') + path,
                         stdout=subprocess.PIPE).stdout
        if live.read_bytes() == original:
            continue
        incoming = m.run('git', '-C', new_repo, 'show', args.source_tag + ':' + new_manifest.get('payload_root', 'payload') + path,
                         stdout=subprocess.PIPE).stdout
        if incoming != original:
            raise ValueError('local skill conflicts with changed vendor body: ' + name)
        additions.extend(['.claude/skills-bundle/' + name + '/', '.claude/skills/' + name + '/'])
    if additions:
        preserve = root / '.claude/.dogany-preserve'
        text = preserve.read_text() if preserve.exists() else ''
        entries = {line.split('#', 1)[0].strip() for line in text.splitlines()}
        missing = [entry for entry in additions if entry not in entries]
        if missing:
            m.atomic(preserve, text.rstrip('\n') + '\n# Local skill overrides retained during pack switch\n' + '\n'.join(missing) + '\n')


def legacy_reader(root):
    """A dated legacy file owns its filename; frontmatter may cite a peer ID.

    Keep this narrow delivery adapter in the local layer until the kit adopts
    the rule. Never rewrite ticket files, or let the kit's prefix ownership
    filter silently discard a historical file belonging to this member.
    """
    rel = 'routines/bundle/devkit_ticket.py'
    path = root / rel
    slug = m.conf(root / '.instance.conf')['DOGANY_AGENT_NAME']
    old = "    label = fields.get('id') or (match.group(1) if match else path.stem)"
    new = "    label = path.stem if name == %r and DATE.match(path.stem) else fields.get('id') or (match.group(1) if match else path.stem)" % slug
    text = path.read_text()
    if new not in text:
        if text.count(old) != 1:
            raise ValueError('legacy ticket adapter: unrecognized kit reader; refusing ticket loss')
        m.atomic(path, text.replace(old, new))
    preserve = root / '.claude/.dogany-preserve'
    text = preserve.read_text() if preserve.exists() else ''
    if rel not in {line.split('#', 1)[0].strip() for line in text.splitlines()}:
        m.atomic(preserve, text.rstrip('\n') + '\n# Dated worklog identity adapter (DGN-2172)\n' + rel + '\n')


def derived_index(root):
    """Resolve migrations beside the installed kit CLI, including isolation."""
    rel = 'routines/bundle/devkit_index.py'
    path = root / rel
    old = "MIGRATIONS = Path(__file__).resolve().parents[2] / 'database/migrations'"
    new = "MIGRATIONS = (Path(__file__).resolve().parents[2] / 'database/kit_dev.py').resolve().parent / 'migrations'"
    text = path.read_text()
    if new not in text:
        if text.count(old) != 1:
            raise ValueError('derived index adapter: unrecognized kit migration resolver')
        text = text.replace(old, new)
    # Legacy files predate the kit status enum. Normalize the projection,
    # keeping their original frontmatter and bytes available to read verbs.
    slug = m.conf(root / '.instance.conf')['DOGANY_AGENT_NAME']
    status_line = "    status = fm.get('status') or 'open'"
    status_adapter = status_line + "\n" + "\n".join([
        "    if ticket['lane'] == %r and DATE.match(ticket['label']) and fm.get('written_by') != 'devkit':" % slug,
        "        legacy_status = status",
        "        head = status.split(' (', 1)[0] if isinstance(status, str) else 'open'",
        "        status = {'in_progress': 'wip', 'superseded': 'rejected', 'draft': 'open', 'closed': 'done'}.get(head, head)",
        "        if status not in STATES | {'dismissed', 'backlog'}:",
        "            status = 'open'",
        "        if status != legacy_status:",
        "            findings.append(issue('TICKET_STATUS_LEGACY_MAPPED', f'{legacy_status} -> {status}', ticket, 'info'))",
    ])
    if status_adapter not in text:
        if text.count(status_line) != 1:
            raise ValueError('derived index adapter: unrecognized legacy status projection')
        text = text.replace(status_line, status_adapter)
    evidence_line = "    evidence = fm.get('done_evidence') or {}"
    evidence_adapter = evidence_line + "\n" + "\n".join([
        "    if ticket['lane'] == %r and DATE.match(ticket['label']) and fm.get('written_by') != 'devkit':" % slug,
        "        evidence = dict(evidence) if isinstance(evidence, dict) else {}",
        "        if status == 'done' and not (evidence.get('kind') and evidence.get('ref')):",
        "            evidence = {'kind': 'file', 'ref': ticket['path']}",
        "            findings.append(issue('TICKET_EVIDENCE_LEGACY_FILE', 'historical completion recorded in the original worklog file', ticket, 'info'))",
    ])
    if evidence_adapter not in text:
        if text.count(evidence_line) != 1:
            raise ValueError('derived index adapter: unrecognized legacy evidence projection')
        text = text.replace(evidence_line, evidence_adapter)
    priority_line = "    priority = fm.get('priority') or None"
    priority_adapter = priority_line + "\n" + "\n".join([
        "    if ticket['lane'] == %r and DATE.match(ticket['label']) and fm.get('written_by') != 'devkit':" % slug,
        "        legacy_priority = priority",
        "        match = re.match(r'^(P[0-3])(?:\\b|$)', str(priority)) if priority is not None else None",
        "        priority = match.group(1) if match else None",
        "        if priority != legacy_priority:",
        "            findings.append(issue('TICKET_PRIORITY_LEGACY_MAPPED', f'{legacy_priority} -> {priority}', ticket, 'info'))",
    ])
    if priority_adapter not in text:
        if text.count(priority_line) != 1:
            raise ValueError('derived index adapter: unrecognized legacy priority projection')
        text = text.replace(priority_line, priority_adapter)
    if path.read_text() != text:
        m.atomic(path, text)
    preserve = root / '.claude/.dogany-preserve'
    text = preserve.read_text() if preserve.exists() else ''
    if rel not in {line.split('#', 1)[0].strip() for line in text.splitlines()}:
        m.atomic(preserve, text.rstrip('\n') + '\n# Isolated derived kit migration resolver (DGN-2172)\n' + rel + '\n')


def register(args, root):
    shared = Path(os.environ.get('DOGANY_SHARED_HOME', str(Path.home() / '.dogany')))
    crews = Path(os.environ.get('DOGANY_CREW_DIR', str(shared / 'crews')))
    # Same locked writer used by installer registration; all peer rows survive.
    m.run('bash', '-c', '. "$1"; _line_upsert "$2" "$3" "" crew',
          'pack-switch', m.LIB / 'line_upsert.sh', crews / args.kit / 'crew.conf',
          'member=' + str(root), env=dict(os.environ, PACK_PY3=os.sys.executable))
    pending = crews / args.kit / 'crew.declared-pending'
    # The first member's conversion can leave this member declared-pending.
    # Promote only this row, under the same lock as the registry writer.
    with open(str(crews / args.kit / 'crew.conf') + '.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if pending.exists():
            lines = pending.read_text().splitlines()
            remaining = [line for line in lines if line != 'member=' + str(root)]
            if remaining != lines:
                m.atomic(pending, ''.join(line + '\n' for line in remaining))
    path = m.lane_file(root)
    # Lock the file itself without a persistent side file on the estate.
    # Re-read after acquiring it; refuse a stale fd after atomic replacement.
    with path.open() as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if os.fstat(handle.fileno()).st_ino != path.stat().st_ino:
            raise ValueError('lane configuration changed during registration; retry')
        data = json.load(handle)
        lane = dict(root=str(root), ticket_dir='worklog', prefix='SKL')
        current = data['lanes'].get(args.slug)
        if current is not None and current != lane:
            raise ValueError('refusing to replace an existing different lane')
        if current is None:
            data['lanes'][args.slug] = lane
            m.atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def isolated_env(tmp, base):
    """Environment for the private-copy check: writes go to a scratch shared
    home, but pack SOURCE lookups still resolve to this machine's real ones.

    The old pack's git coordinate comes from pack-sources.conf or a pack-src
    clone under the shared home; moving the home without carrying those made
    every real switch die with "no local git coordinate" (2026-10-19).
    """
    real = Path(base.get('DOGANY_SHARED_HOME') or Path.home() / '.dogany')
    shared = Path(tmp) / 'shared'
    shared.mkdir(parents=True, exist_ok=True)
    clones = real / 'pack-src'
    if clones.is_dir() and not (shared / 'pack-src').exists():
        (shared / 'pack-src').symlink_to(clones)
    return dict(base, DOGANY_SHARED_HOME=str(shared),
                DOGANY_CREW_DIR=str(shared / 'crews'),
                DOGANY_PACK_SOURCES=base.get('DOGANY_PACK_SOURCES')
                or str(real / 'pack-sources.conf'))


def preflight(args, root, source):
    """Run normal kit gates on the member, then migration gates on a copy.

    Installing the kit into a private copy supplies genuine evidence for the
    migration preflight. No fake receipt is accepted and no live kit is joined.
    User worklog, identity, secrets and memories are never copied or changed.
    """
    kit_install(args, root, source, os.environ, dry=True)
    with tempfile.TemporaryDirectory(prefix='pack-switch-check-') as tmp:
        tmp = Path(tmp)
        planned = tmp / 'member'
        planned.mkdir()
        for name in ('.instance.conf', '.claude/settings.json', '.claude/settings.local.json',
                     '.claude/.dogany-preserve', '.claude/skills', '.claude/skills-bundle',
                     '.claude/packs-migrated', '.claude/packs-retired',
                     'config', 'service', 'routines', 'database', 'PROFILE.md', 'AGENT.md'):
            original = root / name
            if os.path.lexists(original):
                m.copy_path(original, planned / name)
        # Settings and absolute activation links belong to the copied member.
        for name in ('settings.json', 'settings.local.json'):
            path = planned / '.claude' / name
            if path.is_file():
                path.write_text(path.read_text().replace(str(root), str(planned)))
        for path in list(planned.rglob('*')):
            if path.is_symlink() and os.readlink(path).startswith(str(root) + '/'):
                target = os.readlink(path).replace(str(root), str(planned), 1)
                path.unlink()
                path.symlink_to(target)
        env = isolated_env(tmp, os.environ)
        # The dependency is a derived kit: no live/shared writable DB is used.
        kit_install(args, planned, source, env)
        legacy_reader(planned)
        derived_index(planned)
        saved = dict(os.environ)
        try:
            os.environ.update(env)
            preserve_local_skills(args, planned)
            m.migrate(migration(args, planned, dry=True))
        finally:
            os.environ.clear()
            os.environ.update(saved)


def switch(args):
    root = Path(args.root).resolve()
    args.kit = args.join_tag.split('/')[1]
    if args.join_tag != 'pack/%s/v%s' % (args.kit, args.join_tag.split('/v')[-1]):
        raise ValueError('invalid kit join tag')
    manifest = m.git_manifest(m.coordinate(args.catalog, args.new), args.source_tag)
    required = manifest.get('requires_kit') or {}
    if required.get('kit') != args.kit:
        raise ValueError('join tag does not belong to the replacement dependency')
    version = args.join_tag.split('/v')[1]
    if m.join_crew(args.kit) != version:
        raise ValueError('stale switch plan: existing crew version changed')
    marker = root / '.claude/packs-migrated' / (args.old + '-to-' + args.new + '.switch.json')
    intent = dict(old=args.old, new=args.new, tag=args.source_tag, kit_tag=args.join_tag)
    recorded = json.loads(marker.read_text()) if marker.exists() else None
    if recorded and recorded != intent:
        raise ValueError('switch recovery requires the original plan')
    complete = marker.with_suffix('.complete')
    if complete.exists() and json.loads(complete.read_text()) == intent and not m.mounted(root, args.old):
        if (m.mounted(root, args.new) != args.source_tag.split('/v')[1] or
                m.mounted(root, args.kit) != version or not m.evidence(root, args.new, m.mounted(root, args.new)) or
                not m.evidence(root, args.kit, version)):
            raise ValueError('completed switch landing no longer verifies')
        if not args.dry_run:
            register(args, root)
        print('SWITCH already complete')
        return
    # A resumed run may already have the kit. Both cases still require evidence.
    join = m.dependency(args.catalog, manifest, root)
    if join != args.join_tag and not (recorded and m.mounted(root, args.kit) == version):
        raise ValueError('stale switch plan: kit crew or member state changed')
    source = m.coordinate(args.catalog, args.kit)
    km = m.git_manifest(source, args.join_tag)
    if km.get('capabilities', {}).get('db_derived') is not True:
        raise ValueError('only derived kit joins are supported by a member switch')
    # Pin both source objects across forward recovery, not merely tag names.
    pins = {pid: m.output('git', '-C', repo, 'rev-parse', tag + '^{commit}')
            for pid, repo, tag in [(args.new, m.coordinate(args.catalog, args.new), args.source_tag),
                                   (args.kit, source, args.join_tag)]}
    pin_file = marker.with_suffix('.sources.json')
    if pin_file.exists() and json.loads(pin_file.read_text()) != pins:
        raise ValueError('switch source content changed; recovery refused')
    with tempfile.TemporaryDirectory(prefix='pack-switch-source-') as tmp:
        incoming = Path(tmp) / 'kit'
        m.source_checkout(source, args.join_tag, incoming)
        if m.mounted(root, args.old):
            preflight(args, root, incoming)
        elif not recorded:
            raise ValueError('old pack missing before switch')
        if args.dry_run:
            print('SWITCH DRY-RUN OK: kit + crew join + migration + legacy worklog lane')
            return
        m.atomic(pin_file, json.dumps(pins))
        m.atomic(marker, json.dumps(intent))
        preserve_local_skills(args, root)
        kit_install(args, root, incoming, os.environ)
        legacy_reader(root)
        derived_index(root)
        m.migrate(migration(args, root))
        register(args, root)
        if (m.mounted(root, args.old) or m.mounted(root, args.new) != args.source_tag.split('/v')[1]
                or m.mounted(root, args.kit) != version or not m.evidence(root, args.kit, version)):
            raise ValueError('switch landing did not verify')
        m.atomic(marker.with_suffix('.complete'), json.dumps(intent))
        marker.unlink()
        print('SWITCH DONE: kit + crew membership + legacy worklog lane verified')


def main():
    parser = argparse.ArgumentParser()
    for key in ('slug', 'root', 'old', 'new', 'source_tag', 'join_tag', 'catalog'):
        parser.add_argument('--' + key.replace('_', '-'), required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        fd = os.open(args.root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            switch(args)
        finally:
            os.close(fd)
    except (ValueError, OSError, KeyError, IndexError, subprocess.CalledProcessError) as error:
        print('SWITCH FATAL: %s' % error, file=os.sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
