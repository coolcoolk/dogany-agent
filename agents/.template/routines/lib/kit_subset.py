"""Kit-owned delivery selection (DGN-1840). No skill names live here.

Two sets per install. ACTIVATE (activation links + evidence anchors) is
what the owner's state switches on. DELIVER (skills-bundle bodies + the
declared config/packs/<id>.skills) is a superset, so a later "skill on" by
dogany-lifekit-setup is a link, never a reinstall; the declared record keeps
covering every body this pack owns (retire and crew-canonical guards).
"""
import argparse
import json
from pathlib import Path
import re
import sys

from conf_reader import conf_get
from life_engine import KNOWN_STEMS, resolve_conf

# Kit conf LIFEKIT values that carry no owner choice yet ('' = key/file absent).
# The setup skill writes pending -> offered -> on; nothing is switched on here.
UNCHOSEN = ('pending', 'offered', '')


def _directives(bundle_text, names):
    """Parse @ rows: (has_directives, always_on, {domain: names}). Refuses
    unknown/malformed directives; regular item rows (incl. routine rows) are
    deliberately not parsed here."""
    always, domains = set(), {}
    directives = False
    for raw in bundle_text.splitlines():
        line = raw.strip()
        if not line.startswith('@'):
            continue
        directives = True
        fields = line.split('\t')
        if fields[0] == '@always-on' and len(fields) == 2:
            target, name = always, fields[1]
        elif fields[0] == '@domain-subset' and len(fields) == 3:
            domain, name = fields[1:]
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', domain):
                raise ValueError('invalid domain token: ' + domain)
            target = domains.setdefault(domain, set())
        else:
            raise ValueError('unknown or malformed bundle directive: ' + line)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', name) or name not in names:
            raise ValueError('directive names an absent/invalid payload skill: ' + name)
        target.add(name)
    return directives, always, domains


def select_skills(bundle_text, names, conf, domain_tokens=(), is_domain=False, has_pack=True,
                  required_domains=()):
    """Return the ACTIVATE set: the legacy full payload or the selected set.

    Unknown/malformed directives, an unknown LIFEKIT value and ambiguous
    domains refuse delivery.

    DGN-1910 (locked spec 3.6): a packless domain member (a crew staff agent
    minted by the leader: is_domain, no pack behind it, no @domain-subset
    match) selects by its own BUNDLE_<ID>=on keys under LIFEKIT=off, or
    carries KIT_SUBSET=none for an empty set. The precedence lives here, in
    the reader, because BUNDLE keys are written by skill prose. has_pack
    defaults to True so a caller that does not know stays on today's path.
    """
    names = set(names)
    directives, always, domains = _directives(bundle_text, names)
    if not directives:
        return names
    required = set()
    for tokens in required_domains:
        matches = set(tokens) & domains.keys()
        if len(matches) != 1:
            raise ValueError('required pack subset must resolve exactly once; matches='
                             + repr(sorted(matches)))
        required.update(domains[matches.pop()])
    state = conf.get('LIFEKIT', '')
    sentinel = conf.get('KIT_SUBSET', '') or ''
    if sentinel and sentinel != 'none':
        raise ValueError('KIT_SUBSET: unknown value ' + repr(sentinel) + ' (only none is valid)')
    matches = set(domain_tokens) & domains.keys()
    packless = bool(is_domain) and not has_pack and not matches
    if sentinel and not packless:
        raise ValueError('KIT_SUBSET is only valid on a packless domain member')
    if packless and state == 'on':
        raise ValueError('packless staff member cannot take LIFEKIT=on')
    if packless and state == 'off':
        picked = {n for n in names - always
                  if conf.get('BUNDLE_' + n.replace('-', '_').upper()) == 'on'}
        if picked:
            if sentinel:
                sys.stderr.write('kit_subset: sentinel superseded by BUNDLE keys\n')
            return picked
        if sentinel == 'none':
            return set()
        # The old wording is kept as a suffix: callers and logs match on it.
        raise ValueError('packless domain member selects no kit skill'
                         ' (domain subset must resolve exactly once; matches=[])')
    if state == 'on':
        return required | always | {n for n in names
                         if conf.get('BUNDLE_' + n.replace('-', '_').upper()) == 'on'}
    if required_domains and (state == 'off' or state in UNCHOSEN):
        return required
    if state == 'off' and is_domain:
        if len(matches) != 1:
            raise ValueError('domain subset must resolve exactly once; matches=' + repr(sorted(matches)))
        return domains[matches.pop()]
    if state == 'off' or state in UNCHOSEN:
        return set()
    raise ValueError('directive kit: unknown LIFEKIT value ' + repr(state)
                     + ' (expected on|off|pending|offered or absent)')


def deliver_skills(bundle_text, names, conf, domain_tokens=(), is_domain=False, has_pack=True,
                   required_domains=()):
    """Return (DELIVER, ACTIVATE). DELIVER = every payload skill (each one can
    be switched on by LIFEKIT=on + BUNDLE_<ID>=on or is @always-on), except a
    LIFEKIT=off domain instance, which receives exactly its subset."""
    names = set(names)
    active = select_skills(bundle_text, names, conf, domain_tokens, is_domain, has_pack,
                           required_domains)
    if conf.get('LIFEKIT', '') == 'off' and is_domain:
        return set(active), active  # no directives: active is the full payload
    return names, active


def _instance_inputs(root, kit, names, catalog, parent_pack=''):
    """(conf, domain tokens, is_domain, has_pack) from one conf read.

    has_pack (DGN-1910): any DOGANY_PACKS id or parent_pack that is NOT a
    catalog kind=kit row. An id the catalog does not know counts as a pack,
    so an unreadable mount fails closed onto today's domain-subset path."""
    root = Path(root)
    conf_path = (resolve_conf(str(root)) if kit in KNOWN_STEMS else None)
    conf_path = conf_path or str(root / 'config' / (kit + '.conf'))
    keys = ['LIFEKIT', 'KIT_SUBSET'] + ['BUNDLE_' + n.replace('-', '_').upper() for n in names]
    conf = {k: conf_get(k, conf_path) for k in keys}
    instance = str(root / '.instance.conf')
    ids = {t.split('@', 1)[0] for t in (conf_get('DOGANY_PACKS', instance) or '').split(',') if t}
    if parent_pack:
        ids.add(parent_pack)
    tokens = set(ids)
    kits = set()
    for row in catalog.get('packs', []):
        if row.get('kind') == 'kit':
            kits.add(row.get('id'))
        elif row.get('id') in ids:
            tokens.update(row.get('domains') or [])
    has_pack = any(i not in kits for i in ids)
    return conf, tokens, conf_get('DOGANY_AGENT_CLASS', instance) == 'domain', has_pack


def _required_domains(root, kit, catalog, parent_pack='', parent_manifest=''):
    """Read dependencies only for installed packs (or the in-flight parent).

    Older installs with i18n layers already recorded requires_kit as 'above'.
    Keep that compatibility reader until those installs acquire a manifest.
    Catalog domains name subsets; they never imply a kit dependency.
    """
    root = Path(root)
    ids = {t.split('@', 1)[0] for t in
           (conf_get('DOGANY_PACKS', str(root / '.instance.conf')) or '').split(',') if t}
    if parent_pack:
        ids.add(parent_pack)
    rows = {r.get('id'): r for r in catalog.get('packs', [])}
    required = []
    for pack in sorted(ids):
        if rows.get(pack, {}).get('kind') == 'kit':
            continue
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', pack):
            raise ValueError('invalid installed pack id: ' + pack)
        path = root / 'config/packs' / (pack + '.manifest.json')
        if pack == parent_pack and parent_manifest:
            path = Path(parent_manifest)
        dependency = None
        if path.is_file():
            manifest = json.loads(path.read_text())
            if not isinstance(manifest, dict):
                raise ValueError('installed manifest is not an object: ' + str(path))
            if manifest.get('id') != pack:
                raise ValueError('installed manifest id mismatch: ' + str(path))
            need = manifest.get('requires_kit')
            if isinstance(need, dict) and manifest.get('kind') == 'pack':
                dependency = need.get('kit')
        else:
            order = root / 'config/packs' / (pack + '.i18n_order')
            if order.is_file() and ('above ' + kit) in order.read_text().splitlines():
                dependency = kit
        if dependency == kit:
            required.append({pack} | set(rows.get(pack, {}).get('domains') or []))
    return required


def instance_selection(root, kit, bundle_text, names, catalog, parent_pack='', parent_manifest=''):
    """ACTIVATE set for an instance (what the audit compares live links to)."""
    conf, tokens, is_domain, has_pack = _instance_inputs(root, kit, names, catalog, parent_pack)
    return select_skills(bundle_text, names, conf, tokens, is_domain, has_pack,
                         _required_domains(root, kit, catalog, parent_pack, parent_manifest))


def instance_plan(root, kit, bundle_text, names, catalog, parent_pack='', parent_manifest=''):
    """(DELIVER, ACTIVATE) for an instance, from one conf read."""
    conf, tokens, is_domain, has_pack = _instance_inputs(root, kit, names, catalog, parent_pack)
    return deliver_skills(bundle_text, names, conf, tokens, is_domain, has_pack,
                          _required_domains(root, kit, catalog, parent_pack, parent_manifest))


def activate_required(root, manifest, catalog, dry_run=False):
    """Link already delivered dependency skills. Never touch kit controls.

    A satisfied requires_kit skips the kit sub-install. Its pending/off
    skill bodies still need activation when the domain pack is installed.
    Validate every target and collision before creating any link.
    """
    need = manifest.get('requires_kit')
    if manifest.get('kind') != 'pack' or not isinstance(need, dict):
        return
    kit = need.get('kit')
    if not isinstance(kit, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', kit):
        raise ValueError('invalid required kit: ' + repr(kit))
    root = Path(root)
    bundle = root / 'service' / kit / 'bundle.conf'
    text = bundle.read_text() if bundle.is_file() else ''
    if not any(line.strip().startswith('@') for line in text.splitlines()):
        return  # Legacy kits already activate their whole payload at install.
    body = root / '.claude/skills-bundle'
    names = {p.name for p in body.iterdir() if p.is_dir()} if body.is_dir() else set()
    # Limit names to the kit's ownership record, never another pack's body.
    record = root / 'config/packs' / (kit + '.skills')
    owned = {line for line in record.read_text().splitlines()
             if line and not line.startswith('#')}
    rows = {r.get('id'): r for r in catalog.get('packs', [])}
    pack = manifest['id']
    tokens = {pack} | set(rows.get(pack, {}).get('domains') or [])
    # Off domain installs store only their subset. Other valid directives
    # can name skills that were deliberately never delivered here.
    roster = owned | {line.strip().split('\t')[-1] for line in text.splitlines()
                      if line.strip().startswith('@')}
    required = select_skills(text, roster, {'LIFEKIT': 'off'}, tokens, True, True, [tokens])
    links = root / '.claude/skills'
    for name in sorted(required):
        target, link = body / name, links / name
        if name not in owned or name not in names or not (target / 'SKILL.md').is_file():
            raise ValueError('required kit skill body missing: ' + name)
        if (link.exists() or link.is_symlink()) and link.resolve() != target.resolve():
            raise ValueError('required kit skill link collision: ' + name)
    if not dry_run and required:
        links.mkdir(parents=True, exist_ok=True)
    for name in sorted(required):
        link = links / name
        if not dry_run and not link.is_symlink():
            link.symlink_to('../skills-bundle/' + name)
        print('required-kit-skill=' + name)


def main():
    p = argparse.ArgumentParser()
    for key in ('root', 'catalog'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--payload')
    p.add_argument('--kit')
    p.add_argument('--parent-pack', default='')
    p.add_argument('--parent-manifest', default='')
    p.add_argument('--activate-required')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.activate_required:
        try:
            activate_required(args.root, json.loads(Path(args.activate_required).read_text()),
                              json.loads(Path(args.catalog).read_text()), args.dry_run)
        except (OSError, ValueError) as exc:
            p.exit(1, 'kit subset: ' + str(exc) + '\n')
        return
    if not args.payload or not args.kit:
        p.error('--payload and --kit are required for selection')
    payload = Path(args.payload)
    names = {d.name for d in (payload / 'skills-bundle').iterdir() if d.is_dir() and not d.is_symlink()} if (payload / 'skills-bundle').is_dir() else set()
    bundle = payload / 'service' / args.kit / 'bundle.conf'
    try:
        text = bundle.read_text() if bundle.is_file() else ''
        deliver, active = instance_plan(args.root, args.kit, text, names,
                                        json.loads(Path(args.catalog).read_text()), args.parent_pack,
                                        args.parent_manifest)
        # One line per delivered skill: "<name>\t<activate|deliver-only>".
        for name in sorted(deliver):
            print(name + '\t' + ('activate' if name in active else 'deliver-only'))
    except (OSError, ValueError) as exc:
        p.exit(1, 'kit subset: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
