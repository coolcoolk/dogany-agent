#!/usr/bin/env python3
"""F02: journaled package rename, composing the normal install/retire gates.

absorbed_by.kit names the absorbing package (kit or pack). No catalog edits,
network fetches, or tag aliases are made here. Snapshot recovery is deliberately
independent of the source repository and available with --restore-migration.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from semver_range import _cli_satisfies, cmp_pack, is_semver_range

LIB = Path(__file__).resolve().parent
INSTALLER = LIB.parent / 'pack_install.sh'
TOKEN = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')


def run(*args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)


def retire_env(namespace):
    # Tells the retire plan that this migration's M5 sweep archives service/<ns>.
    return dict(os.environ, DOGANY_MIGRATE_SERVICE_NS=namespace)


def output(*args):
    return run(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


def conf(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if '=' in line and not line.startswith('#')) if path.is_file() else {}


def mounted(root, pid):
    return dict(x.split('@', 1) for x in conf(root / '.instance.conf').get('DOGANY_PACKS', '').split(',')
                if '@' in x).get(pid, '')


def satisfies(ver, rng):
    return bool(rng) and is_semver_range(rng) and _cli_satisfies(ver, rng) == 0


def coordinate(catalog, pid):
    value = output('bash', '-c', '. "$1"; pack_coord_for "$3" "$2"',
                   'pack-migrate', LIB / 'pack_coords.sh', catalog, pid)
    if not value:
        raise ValueError('no local git coordinate for ' + pid)
    return Path(value)


def rows(catalog):
    data = json.loads(Path(catalog).read_text())['packs']
    result = {r['id']: r for r in data}
    if len(result) != len(data):
        raise ValueError('duplicate catalog ids')
    return result


def declaration(catalog, old, new, version, target, reverse=False):
    catalog_rows = rows(catalog)
    before, after = catalog_rows.get(old, {}), catalog_rows.get(new, {})
    absorber = before.get('absorbed_by') or {}
    replaces = after.get('replaces') or {}
    if before.get('status') != 'retired' or absorber.get('kit') != new:
        raise ValueError('catalog does not retire %s into %s' % (old, new))
    if after.get('status') != 'published' or not satisfies(target, absorber.get('range', '')):
        raise ValueError('absorber not published or target outside absorbed_by.range')
    if not reverse and (replaces.get('id') != old or not satisfies(version, replaces.get('range', ''))):
        raise ValueError('missing replaces declaration or installed version outside replaces.range')
    return before, after


def need_git(path, role):
    try:
        output('git', '-C', path, 'rev-parse', '--is-inside-work-tree')
    except (subprocess.CalledProcessError, OSError):
        raise ValueError('%s pack at %s is not a git work tree; --migrate-pack needs git checkouts '
                         '(local clone of the pack repo) on both the old and new pack side' % (role, path))


def git_manifest(repo, tag):
    return json.loads(output('git', '-C', repo, 'show', tag + ':pack-manifest.json'))


def safe(root, rel):
    p = Path(rel)
    if p.is_absolute() or not p.parts or any(x in ('.', '..') for x in p.parts):
        raise ValueError('unsafe migration path: ' + rel)
    for parent in p.parents:
        if parent != Path('.') and (root / parent).is_symlink():
            raise ValueError('symlink parent in migration path: ' + rel)
    return root / p


def copy_path(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dst.symlink_to(os.readlink(src))
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def counts(root):
    return {name: sum(1 for p in (root / rel).rglob('*') if p.is_file() or p.is_symlink())
            for name, rel in [('records', 'config/packs'), ('skills', '.claude/skills'), ('service', 'service')]}


def evidence(root, pid, version):
    return output('bash', '-c', '. "$1"; evidence_consume "$2" "$3" "$4"',
                  'pack-migrate', LIB / 'install_evidence.sh', root, pid, version).startswith('VERIFIED|')


def touched(root, old_manifest, manifest, old, new, payload):
    # Snapshot only the old and incoming package surfaces, plus shared files
    # the installer merges. Absent destinations are recorded for R2 cleanup.
    paths = {'.instance.conf', 'config/packs',
             '.claude/update-refit', '.telegram_bot/session-inbox',
             'config/' + old + '.conf', 'config/' + new + '.conf',
             'service/' + (old_manifest.get('service_namespace') or old),
             'service/' + (manifest.get('service_namespace') or new),
             '.claude/settings.json', '.claude/settings.local.json', '.claude/.dogany-preserve',
             'PROFILE.md', 'AGENT.md'}
    skill_record = root / ('config/packs/' + old + '.skills')
    skills = set(line.strip() for line in skill_record.read_text().splitlines()
                 if line.strip() and not line.startswith('#')) if skill_record.exists() else set()
    if (payload / 'skills-bundle').is_dir():
        skills.update(p.name for p in (payload / 'skills-bundle').iterdir())
    for name in skills:
        if not TOKEN.fullmatch(name):
            raise ValueError('unsafe skill name in migration: ' + name)
        paths.update(('.claude/skills/' + name, '.claude/skills-bundle/' + name))
        body = root / '.claude/skills-bundle' / name
        if body.is_symlink():
            raise ValueError('external skill body cannot be snapshotted: ' + str(body))
    for directory in ('config', 'routines/bundle', 'knowledge'):
        if (payload / directory).exists():
            paths.update(str(p.relative_to(payload)) for p in (payload / directory).iterdir())
    ledger = root / ('config/packs/' + old + '.files')
    if ledger.exists():
        paths.update(line for line in ledger.read_text().splitlines() if line and not line.startswith('#'))
    for rel in paths:
        safe(root, rel)
    # No nested duplicate snapshot entries (a directory already owns them).
    return sorted(rel for rel in paths if not any(str(p) in paths for p in Path(rel).parents if p != Path('.')))


def skill_drift(root, repo, tag, old_manifest, payload):
    record = root / ('config/packs/' + old_manifest['id'] + '.skills')
    if not record.exists():
        return
    preserve = root / '.claude/.dogany-preserve'
    local = {line.split('#', 1)[0].strip().rstrip('/') for line in preserve.read_text().splitlines()} if preserve.exists() else set()
    for name in record.read_text().splitlines():
        if not name or name.startswith('#'):
            continue
        if not TOKEN.fullmatch(name):
            raise ValueError('unsafe recorded skill name')
        live = safe(root, '.claude/skills-bundle/' + name + '/SKILL.md')
        if {'.claude/skills-bundle/' + name, '.claude/skills/' + name} <= local:
            continue  # explicit local layer; the normal installer holds it too
        if live.is_file() and (payload / 'skills-bundle' / name / 'SKILL.md').is_file():
            original = run('git', '-C', repo, 'show', tag + ':' + old_manifest.get('payload_root', 'payload') +
                           '/skills-bundle/' + name + '/SKILL.md', stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            if live.read_bytes() != original:
                raise ValueError('content-drift gate: locally edited own skill ' + name)


def restore(root, journal, state):
    atomic(journal / 'MIGRATE-INPROGRESS', 'restoring\n')
    aborted = journal / 'aborted'
    # Each path restores independently; an interruption can repeat the whole
    # restore without consuming the only snapshot or overwriting an archive.
    for rel in state['paths']:
        current = safe(root, rel)
        if os.path.lexists(current):
            dest = aborted / rel
            if os.path.lexists(dest):
                dest = aborted / ('retry-' + str(time.time_ns())) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(dest))
        saved = journal / 'snapshot' / rel
        if os.path.lexists(saved):
            copy_path(saved, current)
    atomic(journal / 'RESTORED', 'snapshot restored\n')
    (journal / 'MIGRATE-INPROGRESS').unlink()
    print('MIGRATE rolled back to snapshot: ' + str(journal))


def sweep(root, journal, state, dry=False):
    ns = state['namespace']
    base = safe(root, 'service/' + ns)
    if base.is_symlink():
        raise ValueError('old service namespace is a symlink')
    retired = conf(root / '.claude/packs-retired' / (state['old'] + '.retired'))
    archive = safe(root, retired.get('archive', '.claude/packs-retired/' + journal.name))
    kept = []
    baseline = journal / 'old-service'
    # Retire's ledger may have archived edited service files too. Put those
    # back before the orphan sweep; only tag-identical bytes stay archived.
    saved_service = journal / 'snapshot/service' / ns
    if not dry:
        for saved in saved_service.rglob('*') if saved_service.exists() else []:
            if not saved.is_file() or saved.is_symlink():
                continue
            rel = saved.relative_to(saved_service)
            original = baseline / rel
            live = base / rel
            archived = archive / 'files/service' / ns / rel
            if (not original.is_file() or saved.read_bytes() != original.read_bytes()) and not os.path.lexists(live) and archived.is_file():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(archived), str(live))
    for p in sorted(base.rglob('*')) if base.exists() else []:
        if p.is_dir() and not p.is_symlink():
            continue
        rel = p.relative_to(base)
        original = baseline / rel
        equal = (not p.is_symlink() and original.is_file() and p.read_bytes() == original.read_bytes())
        if equal:
            print('M5 archive service/%s/%s' % (ns, rel))
            if not dry:
                dest = archive / 'files/service' / ns / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(dest):
                    raise ValueError('archive destination exists: ' + str(dest))
                shutil.move(str(p), str(dest))
        else:
            kept.append(str(p.relative_to(root)))
            print('M5 kept_local=' + kept[-1])
    if not dry and base.exists():
        for directory in sorted((p for p in base.rglob('*') if p.is_dir() and not p.is_symlink()),
                                key=lambda p: len(p.parts), reverse=True) + [base]:
            try:
                directory.rmdir()
            except OSError:
                pass
    return str(archive.relative_to(root)), kept


def checkpoint(journal, state, step):
    state['step'] = step
    atomic(journal / 'state.json', json.dumps(state))
    print('M%d complete' % step, flush=True)


def source_checkout(source, tag, dest):
    # Local git checkout preserves object identity, including historical stale
    # seals (G6). Never accept a tar tree as rollback/source provenance.
    output('git', '-C', source, 'rev-parse', '--show-toplevel')
    run('git', 'clone', '--quiet', '--shared', '--no-checkout', source, dest)
    run('git', '-C', dest, 'checkout', '--quiet', '--detach', tag)


def migrate(args):
    root = Path(args.root).resolve()
    old, new = args.old, args.new
    if any('\n' in value or '\r' in value for value in (args.source_tag or '', args.untagged_local or '')):
        raise ValueError('source declaration must be one line')
    if not TOKEN.fullmatch(old) or not TOKEN.fullmatch(new) or old == new:
        raise ValueError('migration requires two distinct valid package ids')
    if not (root / '.instance.conf').is_file():
        raise ValueError('not an instance root')
    base = root / '.claude/packs-migrated'
    receipt = base / (old + '-to-' + new + '.migrated')
    active = [p.parent for p in base.glob(old + '-to-' + new + '.*/MIGRATE-INPROGRESS')]
    if len(active) > 1:
        raise ValueError('multiple migration journals; restore explicitly')
    journal = active[0] if active else None
    state = json.loads((journal / 'state.json').read_text()) if journal else None
    if state and state['source'] != (args.source_tag or args.untagged_local):
        raise ValueError('resume source differs from migration journal')
    if state and (journal / 'MIGRATE-INPROGRESS').read_text().strip() == 'restoring':
        if args.dry_run:
            raise ValueError('snapshot restore pending')
        restore(root, journal, state)
        return migrate(args)
    if not state and receipt.exists() and not mounted(root, old) and mounted(root, new) == conf(receipt)['to'].split('@', 1)[1]:
        print('MIGRATE already complete: ' + str(receipt))
        return
    if state and state['step'] == 1 and ((root / ('config/packs/' + new + '.receipt.inprogress')).exists() or
            ((root / ('config/packs/' + new + '.receipt')).exists() and mounted(root, new) != state['target'])):
        if args.dry_run:
            raise ValueError('interrupted install requires snapshot restore')
        restore(root, journal, state)
        return migrate(args)
    source = coordinate(args.catalog, new)
    manifest = git_manifest(source, args.source_tag) if args.source_tag else json.loads((source / 'pack-manifest.json').read_text())
    if args.source_tag and not args.source_tag.startswith('pack/' + new + '/v'):
        raise ValueError('source tag belongs to another package')
    target = args.source_tag.split('/v', 1)[1] if args.source_tag else manifest['pack_version']
    need_git(source, 'new')
    source_oid = output('git', '-C', source, 'rev-parse', (args.source_tag or 'HEAD') + '^{commit}')
    if state and state['source_oid'] != source_oid:
        raise ValueError('resume source commit differs from migration journal')
    version = state['version'] if state else mounted(root, old)
    if not version:
        raise ValueError("pack '%s' is not installed on this instance (nothing to migrate)" % old)
    reverse_receipt = conf(base / (new + '-to-' + old + '.migrated'))
    reverse = args.catalog_explicit and reverse_receipt.get('from') == new + '@' + target and reverse_receipt.get('to') == old + '@' + version
    declaration(args.catalog, old, new, version, target, reverse)
    if manifest.get('id') != new or manifest.get('kind') != 'pack' or manifest.get('contract_version') != 1:
        raise ValueError('rename requires a contract behavior pack')
    if state and (state['target'] != target or state['source'] != (args.source_tag or args.untagged_local)):
        raise ValueError('resume source differs from migration journal')
    required = manifest.get('requires_kit') or {}
    if required and not satisfies(mounted(root, required.get('kit', '')), required.get('range', '')):
        raise ValueError('install the required kit before an instance-scoped migration')
    for skill in manifest.get('skills', []):
        if isinstance(skill, dict) and skill.get('sharing_mode') == 'share':
            stamp = conf(root / '.claude/skills-bundle' / skill['name'] / '.pack-stamp')
            if stamp.get('pack_version') and cmp_pack(new, stamp['pack_version'], target) > 0:
                raise ValueError('share skill stamp downgrade refused')
    with tempfile.TemporaryDirectory(prefix='pack-migrate-') as tmp:
        tmp = Path(tmp)
        incoming = source
        if args.source_tag:
            incoming = tmp / 'source'
            source_checkout(source, args.source_tag, incoming)
        if state and state['step'] == 1 and evidence(root, new, target):
            if not args.dry_run:
                checkpoint(journal, state, 2)
        if not state:
            old_source = coordinate(args.catalog, old)
            need_git(old_source, 'old')
            old_tag = 'pack/%s/v%s' % (old, version)
            old_manifest = git_manifest(old_source, old_tag)
            skill_drift(root, old_source, old_tag, old_manifest,
                        incoming / manifest.get('payload_root', 'payload'))
        # M0 verifies the source and every normal install gate BEFORE snapshot.
        install = ['bash', INSTALLER, args.slug, root, '--pack', new, '--pack-dir', incoming,
                   '--catalog', args.catalog, '--no-state', '--no-start']
        install += ['--source-tag', args.source_tag] if args.source_tag else ['--untagged-local', args.untagged_local]
        if not state or state['step'] < 2:
            run(*install, '--dry-run')
        if not journal:
            namespace = old_manifest.get('service_namespace') or old
            if not TOKEN.fullmatch(namespace) or namespace == manifest.get('service_namespace'):
                raise ValueError('unsafe or overlapping service namespace')
            stage = tmp / 'journal'
            stage.mkdir()
            old_payload = old_manifest.get('payload_root', 'payload') + '/service/' + namespace
            if not re.fullmatch(r'[a-zA-Z0-9_./-]+', old_payload) or '..' in Path(old_payload).parts:
                raise ValueError('unsafe old payload root')
            entries = output('git', '-C', old_source, 'ls-tree', '-r', old_tag, '--', old_payload)
            for entry in entries.splitlines():
                meta, path = entry.split('\t', 1)
                if meta.split()[0] not in ('100644', '100755'):
                    continue
                dest = stage / 'old-service' / Path(path).relative_to(old_payload)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(run('git', '-C', old_source, 'show', old_tag + ':' + path, stdout=subprocess.PIPE).stdout)
            state = dict(old=old, new=new, version=version, target=target, namespace=namespace,
                         source=args.source_tag or args.untagged_local, source_oid=source_oid, step=1, before=counts(root),
                         paths=touched(root, old_manifest, manifest, old, new, incoming / manifest.get('payload_root', 'payload')))
            for rel in state['paths']:
                path = safe(root, rel)
                if os.path.lexists(path):
                    copy_path(path, stage / 'snapshot' / rel)
            atomic(stage / 'state.json', json.dumps(state))
            atomic(stage / 'MIGRATE-INPROGRESS', 'migrating\n')
            if args.dry_run:
                # M4 against an isolated planned end state: the real installer
                # preflight ran above; only its future evidence is simulated.
                planned = tmp / 'planned'
                shutil.copytree(stage / 'snapshot', planned, symlinks=True)
                cp = planned / 'config/packs'
                cp.mkdir(parents=True, exist_ok=True)
                old_conf = (planned / '.instance.conf').read_text()
                atomic(planned / '.instance.conf', re.sub(r'^DOGANY_PACKS=(.*)$',
                       lambda m: m[0] + ',' + new + '@' + target, old_conf, flags=re.M))
                atomic(cp / (new + '.receipt'), 'schema=1\npack=%s\nversion=%s\nclass=pack\nstatus=delivery-complete\nanchor_count=1\nanchor=.instance.conf\n' % (new, target))
                skills = manifest.get('skills', [])
                atomic(cp / (new + '.skills'), '\n'.join(s['name'] if isinstance(s, dict) else s for s in skills) + '\n')
                run('bash', INSTALLER, args.slug, planned, '--retire', old, '--catalog', args.catalog, '--dry-run', env=retire_env(namespace))
                sweep(root, stage, state, dry=True)
                print('MIGRATE DRY-RUN OK (zero instance writes)')
                return
            base.mkdir(parents=True, exist_ok=True)
            journal = base / (old + '-to-' + new + '.' + str(time.time_ns()))
            shutil.copytree(stage, journal, symlinks=True,
                            ignore=shutil.ignore_patterns('MIGRATE-INPROGRESS'))
            # Marker is published last: an interrupted snapshot is never active.
            atomic(journal / 'MIGRATE-INPROGRESS', 'migrating\n')
            checkpoint(journal, state, 1)
        elif args.dry_run:
            sweep(root, journal, state, dry=True)
            print('MIGRATE resume planned at M%d' % (state['step'] + 1))
            return
        if state['step'] < 2:
            try:
                run(*install)
            except subprocess.CalledProcessError:
                restore(root, journal, state)
                raise
            checkpoint(journal, state, 2)
        if state['step'] < 4:
            try:
                if not evidence(root, new, target):
                    raise ValueError('M3 new install evidence not VERIFIED')
                checkpoint(journal, state, 3)
                run('bash', INSTALLER, args.slug, root, '--retire', old, '--catalog', args.catalog, env=retire_env(state['namespace']))
            except (ValueError, subprocess.CalledProcessError):
                restore(root, journal, state)
                raise
            checkpoint(journal, state, 4)
        archive, kept = sweep(root, journal, state)
        checkpoint(journal, state, 5)
        atomic(receipt, 'schema=1\nfrom=%s@%s\nto=%s@%s\nsource=%s\nretire_archive=%s\nsnapshot=%s\nkept_local=%s\nmigrated_at=%s\n' %
               (old, version, new, target, ('tag:' + args.source_tag) if args.source_tag else 'untagged-local:' + args.untagged_local,
                archive, str((journal / 'snapshot').relative_to(root)), ','.join(kept), time.strftime('%Y-%m-%dT%H:%M:%S')))
        (journal / 'MIGRATE-INPROGRESS').unlink()
        print('MIGRATE DONE counts before=%s after=%s' % (state['before'], counts(root)))


def plan(catalog, old, new, version, root):
    source = coordinate(catalog, new)
    tags = output('git', '-C', source, 'for-each-ref', '--sort=-version:refname',
                  '--format=%(refname:short)', 'refs/tags/pack/' + new + '/v*').splitlines()
    channel = conf(Path(root) / '.instance.conf').get('DOGANY_UPDATE_CHANNEL', 'stable')
    for tag in tags:
        target = tag.split('/v', 1)[1]
        if channel != 'dev' and '-' in target:
            continue
        try:
            declaration(catalog, old, new, version, target)
            manifest = git_manifest(source, tag)
            if manifest.get('id') != new or manifest.get('kind') != 'pack' or manifest.get('contract_version') != 1:
                continue
            fw = conf(Path(root) / '.instance.conf').get('DOGANY_FW_VERSION', '')
            if not satisfies(fw, manifest.get('requires_framework', '')):
                continue
            # An offer must include a satisfiable dependency on THIS member.
            # Never infer membership from a peer's kit candidate.
            dependency(catalog, manifest, Path(root))
        except (ValueError, OSError, KeyError, subprocess.CalledProcessError):
            continue
        return tag
    return ''


def lane_file(root):
    for parent in (root, *root.parents):
        path = parent / 'devkit-lanes.json'
        if path.is_file():
            return path
    raise ValueError('missing devkit lane configuration')


def join_crew(kit):
    shared = Path(os.environ.get('DOGANY_SHARED_HOME', str(Path.home() / '.dogany')))
    crew = Path(os.environ.get('DOGANY_CREW_DIR', str(shared / 'crews'))) / kit
    if (crew / '.lease').exists() or (crew / 'crew.declared').exists():
        raise ValueError('kit crew is leased or still declared')
    members = [Path(line[7:]).resolve() for line in (crew / 'crew.conf').read_text().splitlines()
               if line.startswith('member=')]
    versions = {mounted(peer, kit) for peer in members}
    if not members or len(members) != len(set(members)) or len(versions) != 1 or '' in versions:
        raise ValueError('kit crew has no uniform installed version')
    version = versions.pop()
    if any(not evidence(peer, kit, version) for peer in members):
        raise ValueError('kit crew evidence does not permit joining')
    return version


def dependency(catalog, manifest, root):
    """Return the exact kit join tag, or empty for a verified installed kit.

    First joins are deliberately limited to derived kits: a shared writable
    database requires the existing crew transaction, never a member switch.
    """
    required = manifest.get('requires_kit') or {}
    if not required:
        return ''
    kit, rng = required['kit'], required['range']
    current = mounted(root, kit)
    if current:
        if not satisfies(current, rng) or not evidence(root, kit, current):
            raise ValueError('required kit is not verified in range')
        return ''
    if kit != 'kit_dev':
        raise ValueError('missing kit requires its own crew onboarding transaction')
    source = coordinate(catalog, kit)
    shared = Path(os.environ.get('DOGANY_SHARED_HOME', str(Path.home() / '.dogany')))
    version = join_crew(kit)
    if not satisfies(version, rng):
        raise ValueError('kit crew evidence or range does not permit joining')
    tag = 'pack/%s/v%s' % (kit, version)
    km = git_manifest(source, tag)
    if (km.get('id') != kit or km.get('kind') != 'kit' or
            km.get('capabilities', {}).get('db_derived') is not True or
            not satisfies(conf(root / '.instance.conf').get('DOGANY_FW_VERSION', ''), km.get('requires_framework', ''))):
        raise ValueError('kit join requires a compatible derived kit tag')
    if (shared / 'crews' / kit / (kit + '.db')).exists():
        raise ValueError('derived kit has a contradictory crew database')
    lanes = json.loads(lane_file(root).read_text())['lanes']
    slug = conf(root / '.instance.conf')['DOGANY_AGENT_NAME']
    expected = dict(root=str(root), ticket_dir='worklog', prefix='SKL')
    if slug in lanes and lanes[slug] != expected:
        raise ValueError('existing member lane differs from onboarding declaration')
    return tag


def publish_check(catalog, pid, source):
    catalog_rows = rows(catalog)
    replacement = catalog_rows.get(pid, {}).get('replaces')
    if not replacement:
        return
    old = catalog_rows.get(replacement['id'], {})
    absorber = old.get('absorbed_by') or {}
    if absorber.get('kit') != pid or not is_semver_range(absorber.get('range', '')):
        raise ValueError('replacement has no matching absorbed_by range')
    tags = output('git', '-C', source, 'for-each-ref', '--format=%(refname:short)', 'refs/tags/pack/' + pid + '/v*')
    for tag in tags.splitlines():
        if not satisfies(tag.split('/v', 1)[1], absorber['range']):
            raise ValueError('out-of-line alias tag refused: ' + tag)


def main():
    if sys.argv[1] == 'dependency':
        catalog, new, tag, root = sys.argv[2:]
        print(dependency(catalog, git_manifest(coordinate(catalog, new), tag), Path(root)))
        return
    if sys.argv[1] == 'replacement':
        catalog, old, new = sys.argv[2:]
        row = rows(catalog).get(new, {})
        sys.exit(0 if row.get('kind') == 'pack' or row.get('replaces', {}).get('id') == old else 1)
    if sys.argv[1] == 'plan':
        print(plan(*sys.argv[2:]))
        return
    if sys.argv[1] == 'framework-check':
        catalog, pid, tag, version = sys.argv[2:]
        manifest = git_manifest(coordinate(catalog, pid), tag)
        if not satisfies(version, manifest.get('requires_framework', '')):
            raise ValueError('replacement framework end state incompatible')
        return
    if sys.argv[1] == 'publish-check':
        publish_check(*sys.argv[2:])
        return
    if sys.argv[1] == 'restore':
        root, journal = map(lambda p: Path(p).resolve(), sys.argv[2:])
        journal.relative_to(root / '.claude/packs-migrated')
        fd = os.open(root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            restore(root, journal, json.loads((journal / 'state.json').read_text()))
        finally:
            os.close(fd)
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('slug'); parser.add_argument('root'); parser.add_argument('old'); parser.add_argument('new')
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--catalog-explicit', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--source-tag'); source.add_argument('--untagged-local')
    args = parser.parse_args()
    if args.dry_run:
        migrate(args)
    else:
        # Separate from .instance.conf.lock, which install/retire acquire.
        fd = os.open(args.root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            migrate(args)
        finally:
            os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print('MIGRATE FATAL: %s' % error, file=sys.stderr)
        sys.exit(1)
