"""DGN-2035 / dec-401: split by default; only owner words opt into main.

Match complete request clauses, never a mention of 'one', a question, a
quotation, a hypothetical, or an agent-authored consult question. Unknown
wording stays on the default path. No one-or-split menu is generated.
"""
import re


_REQUESTS = (
    r"(?:please )?(?:do|handle) (?:it all|everything) (?:with|in) (?:just )?one agent",
    r"i (?:want|only want) (?:just )?(?:one|a single) agent(?: (?:for everything|to do everything))?",
    r"i (?:don't|do not) want (?:multiple|several) agents",
    "\uc5d0\uc774\uc804\ud2b8 \\s*\uc5ec\ub7ec \\s*\uac1c(?:\ub294|\uac00)? \\s*\uc2eb\uc5b4(?:\uc694)?",
    "\ud558\ub098\ub85c \\s*\ub2e4 \\s*\ud574(?:\\s*)\uc918(?:\uc694)?",
    "\uc5d0\uc774\uc804\ud2b8 \\s*\ud558\ub098\ub85c \\s*(?:\ub2e4 \\s*)?\ud574(?:\\s*)\uc918(?:\uc694)?",
)


def request_basis(flow):
    """Return the verbatim owner entry and its path, or None.

    Re-evaluate the latest owner entry containing an agent-count request. A
    later explicit split request cancels an earlier single-agent preference.
    """
    entries = [('request', flow.get('request') or '')]
    for key in ('context', 'choice_words'):
        entries.extend((f'{key}.{n}.answer', x.get('answer') or '')
                       for n, x in enumerate(flow.get(key) or []))
    basis = None
    for path, words in entries:
        for clause in re.split(r'[.!\n/]+', words):
            clause = clause.strip()
            if re.fullmatch(r"(?:please )?(?:use|make|create) (?:a separate|multiple|several) agents?"
                            r"|i (?:don't|do not) want (?:one|a single) agent", clause, re.I):
                basis = None
            elif any(re.fullmatch(pattern, clause, re.I) for pattern in _REQUESTS):
                basis = {'path': path, 'quote': words}
    return basis


def install(screens, flow):
    """Delegate pack + requires_kit to the existing installer on main only.

The installer owns dependency versions, source sealing, receipts and kit
payloads. This entry gate enforces one kit before that writer is invoked.
No bot/token, member mint, service start, or minting-state binding is used.
"""
    import json
    import os
    from pathlib import Path
    import tempfile
    from mint_screens import HERE, conf_get, pack_build_version, pack_row_dir

    if screens._conf('.instance.conf', 'DOGANY_AGENT_CLASS') != 'main':
        raise ValueError('single-agent install requires main')
    if flow.get('main_agent') != request_basis(flow) or not flow.get('main_agent'):
        raise ValueError('single-agent request basis missing or changed')
    pack = screens.chosen_pack(flow)
    if not pack:
        raise ValueError('no confirmed pack')
    catalog = screens.catalog_path()
    pkg = pack_row_dir(catalog, pack)
    if pkg is None:
        raise ValueError('pack source unavailable')
    manifest = json.loads((pkg / 'pack-manifest.json').read_text())
    kit = (manifest.get('requires_kit') or {}).get('kit')
    rows = screens._catalog_rows()
    kit_ids = {r['id'] for r in rows if r.get('kind') == 'kit'}
    if not kit or kit not in kit_ids:
        raise ValueError('pack must declare a catalog kit')
    agent_conf = screens.root / 'config/agent.conf'
    selected = conf_get(agent_conf, 'KIT')
    if selected == 'lifekit':
        selected = 'kit_life'
    ledger = screens._conf('.instance.conf', 'DOGANY_PACKS').split(',')
    installed_ids = {e.strip().split('@')[0] for e in ledger if e.strip()}
    if installed_ids - {r['id'] for r in rows}:
        raise ValueError('one-kit rule: installed package kind is unknown')
    installed_kits = installed_ids & kit_ids
    if selected not in ('', 'pending', 'none', kit) or installed_kits - {kit}:
        raise ValueError('one-kit rule: existing kit differs from required kit')
    _, tag = pack_build_version(catalog, pack)
    slug = screens._conf('.instance.conf', 'DOGANY_AGENT_SLUG') or screens.root.name
    command = ['bash', str(HERE.parent / 'pack_install.sh'), slug, str(screens.root),
               '--pack', pack['id'], '--catalog', str(catalog), '--source-tag', tag,
               '--instance-root', str(screens.root), '--no-start', '--no-state']
    result = screens.run(command, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode:
        raise RuntimeError('pack installer failed (rc=%s); inspect install receipts before retry' % result.returncode)
    installed = {e.strip().split('@')[0] for e in
                 screens._conf('.instance.conf', 'DOGANY_PACKS').split(',')}
    if not {kit, pack['id']} <= installed:
        raise RuntimeError('pack installer did not record both pack and kit')
    # KIT= is the runtime selector, distinct from the install ledger. The
    # member pipeline stamps it too; main closes pending/none only AFTER a
    # successful install. Preserve unrelated config and its permissions.
    lines = agent_conf.read_text().splitlines(True) if agent_conf.exists() else []
    body = ''.join(line for line in lines if not line.startswith('KIT='))
    if body and not body.endswith('\n'):
        body += '\n'
    fd, temp = tempfile.mkstemp(prefix='.mint-kit-', dir=agent_conf.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), agent_conf.stat().st_mode & 0o777 if agent_conf.exists() else 0o600)
            out.write(body + 'KIT=' + kit + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, agent_conf)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {'root': str(Path(screens.root).resolve()), 'pack_id': pack['id'], 'kit_id': kit,
            'source_tag': tag, 'basis': flow['main_agent']}
