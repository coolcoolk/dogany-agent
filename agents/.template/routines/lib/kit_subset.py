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


def select_skills(bundle_text, names, conf, domain_tokens=(), is_domain=False):
    """Return the ACTIVATE set: the legacy full payload or the selected set.

    Unknown/malformed directives, an unknown LIFEKIT value and ambiguous
    domains refuse delivery.
    """
    names = set(names)
    directives, always, domains = _directives(bundle_text, names)
    if not directives:
        return names
    state = conf.get('LIFEKIT', '')
    if state == 'on':
        return always | {n for n in names
                         if conf.get('BUNDLE_' + n.replace('-', '_').upper()) == 'on'}
    if state == 'off' and is_domain:
        matches = set(domain_tokens) & domains.keys()
        if len(matches) != 1:
            raise ValueError('domain subset must resolve exactly once; matches=' + repr(sorted(matches)))
        return domains[matches.pop()]
    if state == 'off' or state in UNCHOSEN:
        return set()
    raise ValueError('directive kit: unknown LIFEKIT value ' + repr(state)
                     + ' (expected on|off|pending|offered or absent)')


def deliver_skills(bundle_text, names, conf, domain_tokens=(), is_domain=False):
    """Return (DELIVER, ACTIVATE). DELIVER = every payload skill (each one can
    be switched on by LIFEKIT=on + BUNDLE_<ID>=on or is @always-on), except a
    LIFEKIT=off domain instance, which receives exactly its subset."""
    names = set(names)
    active = select_skills(bundle_text, names, conf, domain_tokens, is_domain)
    if conf.get('LIFEKIT', '') == 'off' and is_domain:
        return set(active), active  # no directives: active is the full payload
    return names, active


def _instance_inputs(root, kit, names, catalog, parent_pack=''):
    root = Path(root)
    conf_path = (resolve_conf(str(root)) if kit in KNOWN_STEMS else None)
    conf_path = conf_path or str(root / 'config' / (kit + '.conf'))
    keys = ['LIFEKIT'] + ['BUNDLE_' + n.replace('-', '_').upper() for n in names]
    conf = {k: conf_get(k, conf_path) for k in keys}
    instance = str(root / '.instance.conf')
    ids = {t.split('@', 1)[0] for t in (conf_get('DOGANY_PACKS', instance) or '').split(',') if t}
    if parent_pack:
        ids.add(parent_pack)
    tokens = set(ids)
    for row in catalog.get('packs', []):
        if row.get('id') in ids and row.get('kind') != 'kit':
            tokens.update(row.get('domains') or [])
    return conf, tokens, conf_get('DOGANY_AGENT_CLASS', instance) == 'domain'


def instance_selection(root, kit, bundle_text, names, catalog, parent_pack=''):
    """ACTIVATE set for an instance (what the audit compares live links to)."""
    conf, tokens, is_domain = _instance_inputs(root, kit, names, catalog, parent_pack)
    return select_skills(bundle_text, names, conf, tokens, is_domain)


def instance_plan(root, kit, bundle_text, names, catalog, parent_pack=''):
    """(DELIVER, ACTIVATE) for an instance, from one conf read."""
    conf, tokens, is_domain = _instance_inputs(root, kit, names, catalog, parent_pack)
    return deliver_skills(bundle_text, names, conf, tokens, is_domain)


def main():
    p = argparse.ArgumentParser()
    for key in ('root', 'payload', 'kit', 'catalog'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--parent-pack', default='')
    args = p.parse_args()
    payload = Path(args.payload)
    names = {d.name for d in (payload / 'skills-bundle').iterdir() if d.is_dir() and not d.is_symlink()} if (payload / 'skills-bundle').is_dir() else set()
    bundle = payload / 'service' / args.kit / 'bundle.conf'
    try:
        text = bundle.read_text() if bundle.is_file() else ''
        deliver, active = instance_plan(args.root, args.kit, text, names,
                                        json.loads(Path(args.catalog).read_text()), args.parent_pack)
        # One line per delivered skill: "<name>\t<activate|deliver-only>".
        for name in sorted(deliver):
            print(name + '\t' + ('activate' if name in active else 'deliver-only'))
    except (OSError, ValueError) as exc:
        p.exit(1, 'kit subset: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
