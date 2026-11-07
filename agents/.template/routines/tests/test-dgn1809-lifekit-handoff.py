#!/usr/bin/env python3
# test-lane: fast
# test-dgn1809-lifekit-handoff.py -- DGN-1809 contract: the framework
# lifekit-setup shrinks to install + hand-off to the kit's first-install flow.
#   (1) onboarding-check lifekit signal injects the kit offer (life_setup.offer)
#       verbatim; kit key missing -> framework lifekit.offer_fallback in the
#       agent lang; never the retired lifekit.offer copy.
#   (2) life_engine.kit_offer resolution order, per language (agent lang,
#       then en): <bundle-dir>/i18n/<lang>.json, config/i18n/<lang>.bundle.json,
#       framework config/i18n/<lang>.json lifekit.offer_fallback; last the
#       in-code OFFER_DEFAULT (== the en fallback). No Korean in code (r2).
#   (3) framework i18n no longer carries lifekit.offer.
#   (4) SKILL.md: install = every skill row on, no routines, hand-off to the
#       kit flow; no per-item walk / "different time" / summary screen /
#       change-time triggers / retired item names; Google connect drains the
#       outbound backlog; gate matches the onboarding offer gate.
#   DGN-2093 S1 adds the fixture-level promises of the retired kit e2e suites
#   (dgn1815 A3-A6/C3/D1/D3, dgn1817 I0/S1-S3): signal step order, --offer
#   per language, en injection, install section, retired phrases, owner copy.
import importlib.util, json, os, re, subprocess, sys, tempfile

for _name in list(os.environ):
    if _name.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        del os.environ[_name]

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.normpath(os.path.join(HERE, '..'))
TEMPLATE = os.path.normpath(os.path.join(ROUTINES, '..'))
REPO = os.path.normpath(os.path.join(TEMPLATE, '..', '..'))
HOOK = os.path.join(ROUTINES, 'onboarding-check.py')
SKILL = os.path.join(REPO, 'skills', 'dogany-lifekit-setup', 'SKILL.md')

spec = importlib.util.spec_from_file_location(
    'life_engine', os.path.join(ROUTINES, 'lib', 'life_engine.py'))
le = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.join(ROUTINES, 'lib'))
spec.loader.exec_module(le)

fails = []
STALE = ('식단·운동·약속', 'diet, workout, and appointment', 'lifekit.offer')
KO_OFFER = '라이프킷을 켜 뒀어요.\n\n어떤 것부터 해보실래요?'
# owner-confirmed fallback copy (2026-10-01)
KO_FALLBACK = '라이프킷을 켰어요. 기록하고 싶은 것부터 말씀해 주세요. 거기서부터 시작할게요.'
EN_FALLBACK = "Lifekit is on. Tell me what you'd like to keep track of, and we'll start there."
FW = {lg: json.load(open(os.path.join(TEMPLATE, 'config', 'i18n', lg + '.json'),
                         encoding='utf-8')) for lg in ('ko', 'en')}


def check(name, ok, detail=''):
    if not ok:
        fails.append('%s %s' % (name, detail))


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(body)


def mk(root, lang='ko', klass='main', bundle_i18n=None, cfg_bundle=None,
       fw=()):
    write(os.path.join(root, 'identity', 'hot.custom.agent.md'), '# AGENT\n')
    write(os.path.join(root, 'config', 'agent.conf'),
          'KIT=kit_life\nAGENT_LANG=%s\n' % lang)
    write(os.path.join(root, 'config', 'kit_life.conf'), 'LIFEKIT=pending\n')
    write(os.path.join(root, '.instance.conf'), 'DOGANY_AGENT_CLASS=%s\n' % klass)
    write(os.path.join(root, 'service', 'kit_life', 'bundle.conf'), '# x\n')
    for lg, val in (bundle_i18n or {}).items():
        write(os.path.join(root, 'service', 'kit_life', 'i18n', lg + '.json'),
              json.dumps({'life_setup.offer': val}, ensure_ascii=False))
    for lg in fw:                            # framework i18n as update.sh lands it
        write(os.path.join(root, 'config', 'i18n', lg + '.json'),
              json.dumps(FW[lg], ensure_ascii=False))
    for lg, val in (cfg_bundle or {}).items():
        write(os.path.join(root, 'config', 'i18n', lg + '.bundle.json'),
              json.dumps({'life_setup.offer': val}, ensure_ascii=False))


def hook_ctx(root):
    p = subprocess.run([sys.executable, HOOK],
                       input=json.dumps({'cwd': root, 'source': 'startup'}),
                       capture_output=True, text=True, timeout=10)
    try:
        return json.loads(p.stdout)['hookSpecificOutput']['additionalContext']
    except Exception:
        return ''


def injected(ctx):
    m = re.search(r'\n<<<\n(.*)\n>>>\n', ctx or '', re.S)
    return m.group(1) if m else None


# (1) + (2)
with tempfile.TemporaryDirectory() as d:
    r = os.path.join(d, 'a')
    mk(r, bundle_i18n={'ko': KO_OFFER, 'en': 'EN OFFER'})
    ctx = hook_ctx(r)
    check('hook fires', '[lifekit pending]' in ctx)
    check('hook carries kit offer verbatim', KO_OFFER in ctx)
    skill, doc = le.first_install(r)
    check('hook names first-install flow', 'skills-bundle/%s/%s' % (skill, doc) in ctx)
    # A renamed entry is data; the hook must follow both manifest shapes.
    write(os.path.join(r, 'config/packs/kit_life.manifest.json'), json.dumps({
        'pack_version': '2.38.0', 'first_install_skill': 'fixture-entry',
        'first_install_doc': 'guides/start.md'}))
    skill, doc = le.first_install(r)
    check('hook follows declared entry', 'skills-bundle/%s/%s' % (skill, doc) in hook_ctx(r))
    for s in STALE:
        check('hook no stale %r' % s, s not in ctx)
    # Moved from tests/dgn1815_kit_first_install_e2e.py A4 +
    # tests/dgn1817_kit_install_verb_e2e.py I0 (DGN-2093 S1): the lifekit
    # signal orders install step 0 first; LIFEKIT=offered is written only
    # after it, never before installing; every kit skill on, no routine.
    ctx_a = hook_ctx(r)
    check('A4 signal: step 0 first',
          'FIRST its step 0 (scripts/pack/kit_first_install.sh install)' in ctx_a, ctx_a[:400])
    check('A4 signal: offered only after install',
          'only then sets LIFEKIT=offered in config/kit_life.conf' in ctx_a, ctx_a[:600])
    check('A4 signal: skills on, no routine', 'turns every kit skill on (no routine)' in ctx_a)
    check('A4 signal: never offered-first', 'FIRST set LIFEKIT=offered' not in ctx_a)
    check('A3 ko offer injected verbatim', injected(ctx_a) == KO_OFFER, repr(injected(ctx_a)))
    # Moved from tests/dgn1815_kit_first_install_e2e.py A5 (DGN-2093 S1):
    # `life_engine.py <root> --offer ko|en` (the SKILL.md resolver) answers
    # the kit key of exactly that language.
    for lg, want in (('ko', KO_OFFER), ('en', 'EN OFFER')):
        p = subprocess.run([sys.executable, os.path.join(ROUTINES, 'lib', 'life_engine.py'),
                            r, '--offer', lg], capture_output=True, text=True)
        check('A5 CLI --offer %s == kit key' % lg,
              p.returncode == 0 and p.stdout[:-1] == want, repr(p.stdout))
    # Moved from tests/dgn1815_kit_first_install_e2e.py A6 (DGN-2093 S1):
    # AGENT_LANG=en -> the kit's en offer is the injected verbatim block.
    r_en = os.path.join(d, 'a_en')
    mk(r_en, lang='en', bundle_i18n={'ko': KO_OFFER, 'en': 'EN OFFER'})
    ctx_en = hook_ctx(r_en)
    check('A6 AGENT_LANG=en: kit en offer injected verbatim',
          injected(ctx_en) == 'EN OFFER', repr(ctx_en[:400]))

    r = os.path.join(d, 'b')                 # kit predates the key, ko
    mk(r, fw=('ko', 'en'))
    ctx = hook_ctx(r)
    check('missing key, ko -> ko framework fallback', KO_FALLBACK in ctx, ctx[:200])
    check('missing key, ko -> no English default', le.OFFER_DEFAULT not in ctx)
    for s in STALE:
        check('fallback no stale %r' % s, s not in ctx)

    r = os.path.join(d, 'b2')                # kit predates the key, en
    mk(r, lang='en', fw=('ko', 'en'))
    ctx = hook_ctx(r)
    check('missing key, en -> en framework fallback', EN_FALLBACK in ctx, ctx[:200])
    check('kit_offer en -> en fallback', le.kit_offer(r, 'en') == EN_FALLBACK)

    r = os.path.join(d, 'b3')                # ko instance, only en framework file
    mk(r, fw=('en',))
    check('ko fallback missing -> en fallback', le.kit_offer(r, 'ko') == EN_FALLBACK)

    r = os.path.join(d, 'b4')                # no i18n files at all
    mk(r)
    ctx = hook_ctx(r)
    check('no i18n -> code default (hook)', le.OFFER_DEFAULT in ctx, ctx[:200])
    check('no i18n -> code default', le.kit_offer(r, 'ko') == le.OFFER_DEFAULT)
    check('code default == en fallback', le.OFFER_DEFAULT == EN_FALLBACK)

    r = os.path.join(d, 'b5')                # agent-lang fallback beats kit en
    mk(r, bundle_i18n={'en': 'EN OFFER'}, fw=('ko', 'en'))
    check('ko framework fallback before kit en', le.kit_offer(r, 'ko') == KO_FALLBACK)
    r = os.path.join(d, 'b6')                # kit key beats framework fallback
    mk(r, cfg_bundle={'ko': KO_OFFER}, fw=('ko', 'en'))
    check('kit key before framework fallback', le.kit_offer(r, 'ko') == KO_OFFER)

    r = os.path.join(d, 'c')                 # only en in bundle -> en
    mk(r, bundle_i18n={'en': 'EN OFFER'})
    check('lang missing -> kit en', le.kit_offer(r, 'ko') == 'EN OFFER')
    r = os.path.join(d, 'd')                 # config bundle copy only
    mk(r, cfg_bundle={'ko': KO_OFFER})
    check('config/i18n/<lang>.bundle.json read', le.kit_offer(r, 'ko-KR') == KO_OFFER)
    r = os.path.join(d, 'e')
    mk(r, bundle_i18n={'ko': '라이프 A'}, cfg_bundle={'ko': '라이프 B'})
    check('bundle-dir copy wins', le.kit_offer(r, 'ko') == '라이프 A')
    p = subprocess.run([sys.executable, os.path.join(ROUTINES, 'lib', 'life_engine.py'),
                        r, '--offer'], capture_output=True, text=True)
    check('CLI --offer (AGENT_LANG)', p.returncode == 0 and p.stdout.strip() == '라이프 A',
          repr(p.stdout))

    # gate: the offer fires on a domain with KIT=kit_life -> the skill gate
    # must admit exactly that case (no offer-then-refusal)
    r = os.path.join(d, 'f')
    mk(r, klass='domain')
    check('domain + KIT=kit_life -> offer fires', '[lifekit pending]' in hook_ctx(r))

# (3)
for lg in ('ko', 'en'):
    data = json.load(open(os.path.join(TEMPLATE, 'config', 'i18n', lg + '.json'),
                          encoding='utf-8'))
    check('i18n %s: lifekit.offer retired' % lg, 'lifekit.offer' not in data)
check('i18n ko: offer_fallback owner copy', FW['ko'].get('lifekit.offer_fallback') == KO_FALLBACK)
check('i18n en: offer_fallback owner copy', FW['en'].get('lifekit.offer_fallback') == EN_FALLBACK)
src = open(os.path.join(ROUTINES, 'lib', 'life_engine.py'), encoding='utf-8').read()
check('life_engine.py: no Korean in code', not re.search('[\uac00-\ud7a3]', src))

# (4)
s = open(SKILL, encoding='utf-8').read()
low = s.lower()
for bad in ('different time', 'one-screen summary', 'diet-log', 'task-update',
            'diet/workout/appointment', 'workout-log', 'briefing time"',
            '시간 바꿔줘', 'lifekit.offer', 'one at a time (skills first'):
    check('SKILL.md has no %r' % bad, bad.lower() not in low)
check('SKILL.md offer from life_setup.offer', 'life_setup.offer' in s)
check('SKILL.md offer via life_engine --offer', 'life_engine.py <agent-root> --offer' in s)
check('SKILL.md hands off to kit first-install', 'kit setup flow' in s)
check('SKILL.md install: routines untouched', re.search(r'Routine rows: untouched', s) is not None)
check('SKILL.md step 8 drains backlog', 'python3 mirror/reconcile.py --repair --drain' in s)
# Moved from tests/dgn1815_kit_first_install_e2e.py C3 (DGN-2093 S1): the
# install procedure section itself (not just anywhere in the file) turns
# skill rows on, leaves routine rows alone, writes LIFEKIT=on, hands off.
inst = s.split('## procedure: install', 1)[-1].split('\n## ', 1)[0]
check('C3 SKILL.md install section: kind=skill, routines untouched, LIFEKIT=on, hand off',
      '## procedure: install' in s and 'kind=skill' in inst and 'Routine rows: untouched' in inst
      and '`LIFEKIT=on`' in inst and ('hand off' in inst or 'hand over' in inst))
# Moved from tests/dgn1815_kit_first_install_e2e.py D1 (DGN-2093 S1): the
# retired framework phrases (DGN-1809 framework line 1, dev.69 text).
for what, rx in (
        ('per-item walkthrough', r'present items ONE at a time|walks?\s+through\s+the\s+bundle\s+items|numbered options per item'),
        ('"different time" choice', r'different time|\ub2e4\ub978 \uc2dc\uac04'),
        ('one-screen summary', r'one-screen summary'),
        ('change-time trigger', r'change my morning briefing time|'
                                r'\uc544\uce68 \ube0c\ub9ac\ud551 \uc2dc\uac04 \ubc14\uafd4\uc918'),
        ('lifekit.offer key', r'lifekit\.offer(?!_fallback)')):
    hit = re.search(rx, s)
    check('D1 SKILL.md: no %s' % what, hit is None, hit and hit.group(0))
# Moved from tests/dgn1815_kit_first_install_e2e.py D3 (DGN-2093 S1): no
# code path reads the retired lifekit.offer key as a quoted literal.
for rel in (HOOK, os.path.join(ROUTINES, 'lib', 'life_engine.py')):
    hit = re.search(r"""['"]lifekit\.offer['"]""", open(rel, encoding='utf-8').read())
    check('D3 %s reads no retired lifekit.offer key' % os.path.basename(rel), hit is None)
# Moved from tests/dgn1817_kit_install_verb_e2e.py S1-S3 (DGN-2093 S1):
# install step 0 calls the verb; no stop-on-empty-bundle, no offered-before-
# install; the dec-229 install-failure owner copy is wired (no placeholder,
# every key named in the skill, ko + en framework i18n carry them).
check('S1 SKILL.md install step 0 calls kit_first_install.sh install',
      '0. kit files' in inst and 'kit_first_install.sh install' in inst)
check('S2 SKILL.md no stop on empty bundle=, no offered before install',
      'installed: stop, say so' not in s and 'BEFORE installing' not in s)
_fail_keys = ['lifekit.install_failed.' + k for k in ('network', 'no_version', 'error', 'tail')]
check('S3 SKILL.md: no owner-copy placeholder', '[OWNER COPY PENDING' not in s)
for key in _fail_keys:
    check('S3 SKILL.md names %s' % key, key in s)
    for lg in ('ko', 'en'):
        check('S3 i18n %s has %s' % (lg, key), bool(FW[lg].get(key)))
# dec-514: the existing skill composes these exact keys on failure.
for lg in ('ko', 'en'):
    for kind in ('network', 'no_version', 'error'):
        notice = FW[lg]['lifekit.install_failed.' + kind]
        if kind != 'network':
            notice += '\n' + FW[lg]['lifekit.install_failed.tail']
        check('failure notice %s/%s has result, reason, alternative' % (lg, kind),
              len(notice.splitlines()) == 3 and all(line.startswith('· ') for line in notice.splitlines()[1:]))
gate = s[s.index('- GATE'):s.index('- FIRST ACTION')]
check('SKILL.md gate admits KIT naming the life kit',
      'KIT=' in gate and 'kit_life' in gate and 'lifekit' in gate)
check('SKILL.md gate cites the onboarding offer', 'onboarding' in gate)

if fails:
    for f in fails:
        print('FAIL', f)
    sys.exit(1)
print('PASS test-dgn1809-lifekit-handoff (all checks)')
