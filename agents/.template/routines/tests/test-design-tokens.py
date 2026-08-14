#!/usr/bin/env python3
# test-design-tokens.py -- self-tests for the design-system token canon
# (routines/lib/design_tokens.py). Spec of record: docs/DESIGN-SYSTEM.md.
#
# Origin: DGN-376 T1 (two-layer token schema, grill M5).
#
# Run: python3 routines/tests/test-design-tokens.py
# Exit: 0 all pass, nonzero any fail (CI-less self-test convention, same as
# test-portfolio-core.py). Release discipline: run this suite as part of the
# release-preflight pass.
# Python 3 stdlib only.

import importlib.util
import json
import os
import re
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.normpath(os.path.join(TESTS_DIR, '..', 'lib'))
MOD_PATH = os.path.join(LIB_DIR, 'design_tokens.py')

_spec = importlib.util.spec_from_file_location('design_tokens', MOD_PATH)
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)

FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print('  ok  %s' % name)
    else:
        print('  FAIL %s %s' % (name, detail))
        FAILURES.append(name)


HEX_RE = re.compile(r'^#[0-9A-F]{6}$')

print('design_tokens self-tests')

# 1. Schema shape: expected themes present, every theme covers SLOTS exactly.
check('themes present',
      set(dt.THEMES) == {'card-dark', 'console-dark', 'console-light',
                         'console-dark-minimal', 'diagram'},
      str(sorted(dt.THEMES)))
for tname, tvals in sorted(dt.THEMES.items()):
    check('slot coverage %s' % tname,
          set(tvals) == set(dt.SLOTS), str(sorted(tvals)))

# 2. Every resolved value is canonical uppercase hex.
all_ok = all(HEX_RE.match(v) for t in dt.THEMES.values() for v in t.values())
check('all theme values canonical hex', all_ok)
check('all brand values canonical hex',
      all(HEX_RE.match(v) for v in dt.BRAND.values()))

# 3. Layer A anchors: the measured incumbent brand identity.
check('brand teal', dt.BRAND['teal'] == '#4ECDC4')
check('brand amber', dt.BRAND['amber'] == '#FFD166')
check('brand coral', dt.BRAND['coral'] == '#FF6B6B')
check('brand navy ground', dt.BRAND['navy'] == '#13132B')

# 4. Layer B derivation: card-dark is pure brand derivation.
card = dt.THEMES['card-dark']
check('card-dark bg = brand navy', card['bg'] == dt.BRAND['navy'])
check('card-dark accent = brand teal', card['accent'] == dt.BRAND['teal'])
check('card-dark derives fully from brand',
      set(card.values()) <= set(dt.BRAND.values()))

# 5. DGN-376 v2: console themes are brand surfaces (the reskin decision --
#    supersedes the M5 alien-register zero-overlap invariant). console-dark
#    wears the navy card family with the devkit crew accent COBALT (D2),
#    computed via tune_accent on the navy-panel surface, never hardcoded.
console = dt.THEMES['console-dark']
check('console-dark bg = brand navy', console['bg'] == dt.BRAND['navy'])
check('console-dark surface = brand navy-panel',
      console['surface'] == dt.BRAND['navy-panel'])
check('console-dark accent = tune_accent(cobalt, navy-panel)',
      console['accent'] == dt.tune_accent(dt.ACCENT_PALETTE['cobalt'],
                                          dt.BRAND['navy-panel']))
check('console-dark accent is cobalt-family, not teal',
      console['accent'] != dt.BRAND['teal'])
check('console-dark status slots = brand family',
      [console[s] for s in ('green', 'yellow', 'red', 'purple', 'orange')]
      == [dt.BRAND[n] for n in ('green', 'amber', 'coral', 'purple',
                                'orange')])
# console-light pins the candidate-C derivation chain: re-derive and compare.
light = dt.THEMES['console-light']
check('console-light bg = navy-pale',
      light['bg'] == dt.BACKGROUNDS['navy-pale']['hex'])
check('console-light surface = card_surface(bg)',
      light['surface'] == dt.card_surface(light['bg']))
check('console-light text = brand navy as ink',
      light['text'] == dt.BRAND['navy'])
for _slot, _hue in [('accent', 'cobalt'), ('green', 'emerald'),
                    ('yellow', 'amber'), ('red', 'rose'),
                    ('purple', 'violet'), ('orange', 'coral')]:
    check('console-light %s = tune_accent(%s)' % (_slot, _hue),
          light[_slot] == dt.tune_accent(dt.ACCENT_PALETTE[_hue],
                                         light['surface']))
# console-dark-minimal pins the candidate-B derivation (ink ground, peri).
mini = dt.THEMES['console-dark-minimal']
check('console-dark-minimal bg = ink',
      mini['bg'] == dt.BACKGROUNDS['ink']['hex'])
check('console-dark-minimal surface = card_surface(bg)',
      mini['surface'] == dt.card_surface(mini['bg']))
check('console-dark-minimal accent = peri base',
      mini['accent'] == dt.ACCENT_PALETTE['peri'])

# 5b. AA invariant (replaces the retired zero-overlap net): for every console
#     theme, text/muted/accent + all 5 status slots clear AA (>=4.5:1) vs the
#     surface, and text clears AA vs the theme's inset ground.
for _tname in ('console-dark', 'console-light', 'console-dark-minimal'):
    _t = dt.THEMES[_tname]
    _surf = dt._hex_to_rgb(_t['surface'])
    _aa_ok = True
    for _slot in ('text', 'muted', 'accent',
                  'green', 'yellow', 'red', 'purple', 'orange'):
        _cr = dt.contrast_ratio(dt._hex_to_rgb(_t[_slot]), _surf)
        if _cr < dt.AA_CONTRAST:
            _aa_ok = False
            print('    sub-AA %s.%s %.2f' % (_tname, _slot, _cr))
    check('AA vs surface: %s' % _tname, _aa_ok)
    check('AA text vs inset: %s' % _tname,
          dt.contrast_ratio(
          dt._hex_to_rgb(_t['text']),
          dt._hex_to_rgb(dt.CONSOLE_EXTRAS[_tname]['inset']))
          >= dt.AA_CONTRAST)

# 5c. CONSOLE_EXTRAS: theme-bound non-slot values; the SLOTS contract stays
#     at 11; wip is derived from the accent (0.30 blend toward the theme's
#     ground pole), never hand-picked.
check('SLOTS stays at 11', len(dt.SLOTS) == 11)
check('extras cover exactly the console themes',
      set(dt.CONSOLE_EXTRAS) == {'console-dark', 'console-light',
                                 'console-dark-minimal'})
for _tname, _ex in sorted(dt.CONSOLE_EXTRAS.items()):
    check('extras keys %s' % _tname,
          set(_ex) == {'inset', 'wip', 'wash_alpha', 'soft_alpha',
                       'chip_alpha', 'faint_alpha'})
check('dark wash alphas 33/22/1C/11',
      [dt.CONSOLE_EXTRAS['console-dark'][k] for k in
       ('wash_alpha', 'soft_alpha', 'chip_alpha', 'faint_alpha')]
      == ['33', '22', '1C', '11'])
check('dark-minimal wash alphas = dark tier',
      {k: v for k, v in dt.CONSOLE_EXTRAS['console-dark-minimal'].items()
       if k.endswith('_alpha')}
      == {k: v for k, v in dt.CONSOLE_EXTRAS['console-dark'].items()
          if k.endswith('_alpha')})
check('light wash alphas 22/1A/14/0D',
      [dt.CONSOLE_EXTRAS['console-light'][k] for k in
       ('wash_alpha', 'soft_alpha', 'chip_alpha', 'faint_alpha')]
      == ['22', '1A', '14', '0D'])
for _tname in sorted(dt.CONSOLE_EXTRAS):
    _t = dt.THEMES[_tname]
    _pole = ((0, 0, 0) if dt.is_dark_surface(_t['surface'])
             else (255, 255, 255))
    _want = dt._rgb_to_hex(dt._blend(dt._hex_to_rgb(_t['accent']), _pole,
                                     dt._WIP_BLEND))
    check('wip derives from accent %s' % _tname,
          dt.CONSOLE_EXTRAS[_tname]['wip'] == _want)

# 5d. resolve_design (D1): coarse user tier -> registered theme. Normalizes
#     strip().lower(); unknown/empty/None/non-string fall back to the default
#     (dark) -- user input NEVER raises. dark-minimal stays unmapped.
check('resolve dark -> console-dark',
      dt.resolve_design('dark')['name'] == 'console-dark')
check('resolve light -> console-light',
      dt.resolve_design('light')['name'] == 'console-light')
check('resolve normalizes case/space',
      dt.resolve_design('  Light ')['name'] == 'console-light')
for _bad in ('', None, 'purple', 'dark-minimal', 123):
    check('resolve fallback %r -> default' % (_bad,),
          dt.resolve_design(_bad)['name'] == 'console-dark')
_r = dt.resolve_design('light')
check('resolve payload shape (name/slots/extras)',
      set(_r) == {'name', 'slots', 'extras'}
      and set(_r['slots']) == set(dt.SLOTS)
      and _r['slots'] == dt.THEMES['console-light']
      and _r['extras'] == dt.CONSOLE_EXTRAS['console-light'])
_r['slots']['bg'] = '#000000'
_r['extras']['inset'] = '#000000'
check('resolve payload copy isolation',
      dt.THEMES['console-light']['bg'] != '#000000'
      and dt.CONSOLE_EXTRAS['console-light']['inset'] != '#000000')
check('dark-minimal registered but unmapped',
      'console-dark-minimal' in dt.THEMES
      and 'console-dark-minimal' not in dt.DESIGN_SETTINGS.values())
check('default setting is dark', dt.DESIGN_DEFAULT == 'dark'
      and dt.DESIGN_SETTINGS[dt.DESIGN_DEFAULT] == 'console-dark')

# 6. Diagram theme: measured ground + at least one brand-family reference.
diagram = dt.THEMES['diagram']
check('diagram bg (measured docs/img)', diagram['bg'] == '#074A5A')

# 7. theme() returns an independent copy.
t1 = dt.theme('card-dark')
t1['bg'] = '#000000'
check('theme() copy isolation', dt.THEMES['card-dark']['bg'] == '#13132B')

# 8. CSS export: one declaration per slot, lowercase hex, matches the
#    measured console :root line shape.
css = dt.to_css_root('console-dark')
check('css root starts', css.startswith(':root {'))
check('css declaration count', css.count('--') == len(dt.SLOTS))
check('css bg line', '--bg: #13132b;' in css)

# 9. JSON export round-trips and carries all groups.
data = json.loads(dt.to_json())
check('json groups',
      set(data) == {'brand', 'fonts', 'themes',
                    'accents', 'backgrounds', 'crew_bg_default',
                    'crew_accent'},
      str(sorted(data)))
check('json theme parity', data['themes'] == dt.THEMES)
check('json accent parity', data['accents'] == dt.ACCENT_PALETTE)
check('json background parity', data['backgrounds'] == dt.BACKGROUNDS)

# 10. Module self-check passes clean and the CLI exit code agrees.
check('internal self-check clean', dt._self_check() == [])
proc = subprocess.run([sys.executable, MOD_PATH], capture_output=True)
check('cli self-check exit 0', proc.returncode == 0,
      proc.stderr.decode(errors='replace'))
proc_json = subprocess.run([sys.executable, MOD_PATH, '--json'],
                           capture_output=True)
check('cli --json parses',
      proc_json.returncode == 0 and
      json.loads(proc_json.stdout.decode()) == data)

# 11. Layer C -- color identity palette shapes.
check('accent palette count 15', len(dt.ACCENT_PALETTE) == 15)
check('background count 8', len(dt.BACKGROUNDS) == 8)
check('all accent values canonical hex',
      all(HEX_RE.match(v) for v in dt.ACCENT_PALETTE.values()))
check('all background values canonical hex',
      all(HEX_RE.match(b['hex']) for b in dt.BACKGROUNDS.values()))
check('backgrounds split 4 dark / 4 light',
      sorted(b['tier'] for b in dt.BACKGROUNDS.values())
      == ['dark'] * 4 + ['light'] * 4)
check('black + white surfaces present',
      dt.BACKGROUNDS['black']['hex'] == '#000000'
      and dt.BACKGROUNDS['white']['hex'] == '#FFFFFF')

# 12. Declared tier agrees with measured luminance for every background.
for _bgname, _bg in sorted(dt.BACKGROUNDS.items()):
    check('tier matches luminance %s' % _bgname,
          dt.is_dark_surface(_bg['hex']) == (_bg['tier'] == 'dark'))

# 13. Mechanical legibility guarantee: every accent x background pair either
#     clears WCAG AA (4.5:1) on its REAL card (card_surface(bg)) OR is a
#     documented exclusion (accent_for -> None). No sub-threshold shade ships.
_guarantee_ok = True
_excluded = []
for _a in dt.ACCENT_PALETTE:
    for _bgname, _bg in dt.BACKGROUNDS.items():
        _card = dt.card_surface(_bg['hex'])
        _hex = dt.accent_for(_a, _bgname)
        if _hex is None:
            _excluded.append((_a, _bgname))
            continue
        if dt.contrast_ratio(dt._hex_to_rgb(_hex),
                             dt._hex_to_rgb(_card)) < dt.AA_CONTRAST:
            _guarantee_ok = False
            print('    weak: %s on %s' % (_a, _bgname))
check('15x8: every pair clears AA 4.5 on real card or is excluded',
      _guarantee_ok)
print('    exclusions (%d): %s' % (len(_excluded), _excluded))
# On the 8 curated backgrounds no pair is excluded (dark cards stay near-black,
# light cards force deep tuning); assert the curated set is fully covered.
check('curated 15x8 fully covered (no exclusions)', _excluded == [])

# 14. Reference anchor table is a GENERATED artifact of accent_for over the
#     default crew background (dark=navy, light=navy-pale) -- the same code path
#     the render uses. No hand-copied hex table; the invariant is that the
#     published anchor equals accent_for AND clears AA on card_surface(bg).
_ref_ok = True
for _a in dt.ACCENT_PALETTE:
    for _tier, _bgname in dt._DEFAULT_BG.items():
        _anchor = dt.reference_anchor(_a, _tier)
        _direct = dt.accent_for(_a, _bgname)
        if _anchor != _direct:
            _ref_ok = False
            print('    anchor != accent_for %s/%s: %s vs %s'
                  % (_a, _tier, _anchor, _direct))
        # and it must clear AA on the real card
        _card = dt.card_surface(dt.BACKGROUNDS[_bgname]['hex'])
        if _anchor is not None and dt.contrast_ratio(
                dt._hex_to_rgb(_anchor), dt._hex_to_rgb(_card)) < dt.AA_CONTRAST:
            _ref_ok = False
            print('    anchor sub-AA %s/%s' % (_a, _tier))
check('reference anchors == accent_for(default bg) and clear AA', _ref_ok)

# 14b. Chip legibility: chip_needs_outline flags a vivid base hue that fails 3:1
#      vs the card (must be drawn with an outline ring). On light cards most base
#      hues need it (e.g. lime near-invisible); on the near-black dark cards most
#      do not. Assert the helper is threshold-consistent.
_chip_ok = True
for _a, _ah in dt.ACCENT_PALETTE.items():
    for _bgname, _bg in dt.BACKGROUNDS.items():
        _card = dt.card_surface(_bg['hex'])
        _cr = dt.contrast_ratio(dt._hex_to_rgb(_ah), dt._hex_to_rgb(_card))
        if dt.chip_needs_outline(_ah, _card) != (_cr < 3.0):
            _chip_ok = False
check('chip_needs_outline agrees with 3:1 threshold', _chip_ok)
# a concrete near-invisible case must flag; a high-contrast case must not.
check('lime chip on navy-pale card needs outline',
      dt.chip_needs_outline(dt.ACCENT_PALETTE['lime'],
                            dt.card_surface(dt.BACKGROUNDS['navy-pale']['hex'])))
check('amber chip on navy card no outline',
      not dt.chip_needs_outline(dt.ACCENT_PALETTE['amber'],
                                dt.card_surface(dt.BACKGROUNDS['navy']['hex'])))

# 14c. validate_background: all 8 curated pass and return a tier; a mid-grey is
#      rejected (dead-band). Loud ValueError, not silent.
_vb_ok = True
for _bgname, _bg in dt.BACKGROUNDS.items():
    try:
        _tier = dt.validate_background(_bg['hex'])
        if _tier != _bg['tier']:
            _vb_ok = False
    except ValueError:
        _vb_ok = False
check('validate_background accepts all 8 curated (correct tier)', _vb_ok)
_midgrey_rejected = False
try:
    dt.validate_background('#808080')
except ValueError:
    _midgrey_rejected = True
check('validate_background rejects mid-grey #808080', _midgrey_rejected)

# 14d. Perceptual distinctiveness gate (CIE76 dE >= DISTINCT_DE). Replaces the
#      old exact-name collision. Enforces leader-vs-domain and domain-vs-domain.
check('accent_distinct rejects exact reuse',
      dt.accent_distinct('cobalt', ['cobalt']) is False)
check('accent_distinct rejects near-duplicate of taken (below dE)',
      # emerald<->teal base dE ~20.5 is > gate; use a guaranteed-close synthetic
      # via crew leader instead: cobalt vs devkit leader (cobalt) below.
      dt.accent_distinct('cobalt', [], crew='devkit') is False)
check('accent_distinct allows a distant hue',
      dt.accent_distinct('rose', ['teal', 'amber', 'cobalt']) is True)
check('accent_distinct domain-vs-domain enforced',
      dt.accent_distinct('teal', ['emerald']) is
      (dt.delta_e(dt.ACCENT_PALETTE['teal'],
                  dt.ACCENT_PALETTE['emerald']) >= dt.DISTINCT_DE))
_loud = False
try:
    dt.accent_distinct('nope', [])
except KeyError:
    _loud = True
check('accent_distinct raises on unknown candidate', _loud)

# 14e. Loud errors on invalid input (never silent teal/False).
for _fn, _label in [
    (lambda: dt.accent_for('nope', 'navy'), 'accent_for bad accent'),
    (lambda: dt.accent_for('teal', 'nope'), 'accent_for bad bg'),
    (lambda: dt.accent_conflicts_with_crew('nope', 'devkit'), 'conflicts bad accent'),
]:
    _raised = False
    try:
        _fn()
    except KeyError:
        _raised = True
    check('loud KeyError: %s' % _label, _raised)

# 15. Collision rule: a domain accent within the distinctiveness dE of its crew
#     leader accent conflicts; a distant one does not; the leader is exempt.
check('lifekit teal collision flagged',
      dt.accent_conflicts_with_crew('teal', 'lifekit') is True)
check('lifekit distant accent ok',
      dt.accent_conflicts_with_crew('rose', 'lifekit') is False)
check('devkit distant accent ok',
      dt.accent_conflicts_with_crew('rose', 'devkit') is False)

# 16. Layer C leader/domain reconciliation (DGN-376).
check('crew_accent devkit', dt.crew_accent('devkit') == 'cobalt')
check('crew_accent lifekit', dt.crew_accent('lifekit') == 'teal')
check('crew_accent unknown None', dt.crew_accent('nope') is None)
check('leader exempt lifekit teal',
      dt.accent_conflicts_with_crew('teal', 'lifekit', is_leader=True) is False)
check('leader exempt devkit cobalt',
      dt.accent_conflicts_with_crew('cobalt', 'devkit', is_leader=True) is False)
check('domain blocked from crew accent',
      dt.accent_conflicts_with_crew('cobalt', 'devkit', is_leader=False) is True)
check('domain non-crew accent ok',
      dt.accent_conflicts_with_crew('violet', 'devkit', is_leader=False) is False)
check('resolve_accent owner override wins',
      dt.resolve_accent(owner_override='rose', own_color='lime',
                        is_leader=True, crew='devkit') == 'rose')
check('resolve_accent leader -> crew accent',
      dt.resolve_accent(is_leader=True, crew='lifekit') == 'teal')
check('resolve_accent domain -> kit color',
      dt.resolve_accent(is_leader=False, crew='devkit',
                        kit_domain_color='violet') == 'violet')
check('resolve_accent empty -> teal', dt.resolve_accent() == 'teal')
check('json carries crew_accent', data['crew_accent'] == dt.CREW_ACCENT)

# 17. Pick gate (DGN-650, DESIGN-SYSTEM R3 row 5): the onboarding /
#     identity-change enforcement point. gate_pick chains validate_background,
#     accent_conflicts_with_crew, accent_distinct and the AA-exclusion tune;
#     FAIL lines carry the reason + remaining valid choices.
_ok, _lines = dt.gate_pick('rose', 'navy', crew='devkit')
check('gate passes a distinct accent on curated bg',
      _ok and _lines[0].startswith('PASS:'), str(_lines))
check('gate PASS carries tier + anchor + chip flag',
      'tier=dark' in _lines[0] and 'anchor=#' in _lines[0]
      and 'chip_outline=' in _lines[0])
_ok, _lines = dt.gate_pick('teal', '#808080')
check('gate rejects dead-band background',
      not _ok and _lines[0].startswith('FAIL BACKGROUND')
      and 'dead-band' in _lines[0], str(_lines))
check('gate dead-band FAIL lists allowed backgrounds',
      'allowed backgrounds:' in _lines[1] and 'navy' in _lines[1])
_ok, _lines = dt.gate_pick('teal', 'zzz')
check('gate rejects malformed background hex',
      not _ok and _lines[0].startswith('FAIL BACKGROUND'))
_ok, _lines = dt.gate_pick('cobalt', 'navy', crew='devkit')
check('gate rejects crew-colliding domain accent',
      not _ok and _lines[0].startswith('FAIL ACCENT-CREW'), str(_lines))
check('gate crew FAIL lists valid accents excluding the collision',
      _lines[1].startswith('valid accents:') and 'cobalt' not in _lines[1])
_ok, _lines = dt.gate_pick('cobalt', 'navy', crew='devkit', is_leader=True)
check('gate exempts the crew leader wearing the crew accent',
      _ok and _lines[0].startswith('PASS:'), str(_lines))
_ok, _lines = dt.gate_pick('cobalt', 'navy', taken=['cobalt'])
check('gate rejects peer accent reuse',
      not _ok and _lines[0].startswith('FAIL ACCENT-PEER'), str(_lines))
_ok, _lines = dt.gate_pick('redish', 'navy')
check('gate rejects out-of-palette accent with valid list',
      not _ok and _lines[0].startswith('FAIL ACCENT-UNKNOWN')
      and _lines[1].startswith('valid accents:'))
_loud = False
try:
    dt.gate_pick('teal', 'navy', crew='nope')
except KeyError:
    _loud = True
check('gate raises on unknown crew (caller error, not user input)', _loud)
# every curated bg x every valid accent choice the gate offers must PASS
_offer_ok = True
for _bgname in dt.BACKGROUNDS:
    for _a in dt._valid_accents(dt.BACKGROUNDS[_bgname]['hex'], crew='devkit'):
        if not dt.gate_pick(_a, _bgname, crew='devkit')[0]:
            _offer_ok = False
            print('    offered-but-rejected: %s on %s' % (_a, _bgname))
check('gate never offers a choice it would then reject', _offer_ok)
# CLI exit contract: 0 allowed / 1 rejected / 2 caller error.
_p = subprocess.run([sys.executable, MOD_PATH, '--gate', '--accent', 'rose',
                     '--bg', 'navy', '--crew', 'devkit'], capture_output=True)
check('gate CLI pass exit 0', _p.returncode == 0
      and _p.stdout.decode().startswith('PASS:'),
      _p.stderr.decode(errors='replace'))
_p = subprocess.run([sys.executable, MOD_PATH, '--gate', '--accent', 'teal',
                     '--bg', '#808080'], capture_output=True)
check('gate CLI dead-band exit 1', _p.returncode == 1
      and 'dead-band' in _p.stdout.decode())
_p = subprocess.run([sys.executable, MOD_PATH, '--gate', '--accent', 'cobalt',
                     '--bg', 'navy', '--crew', 'devkit'], capture_output=True)
check('gate CLI crew-collision exit 1', _p.returncode == 1
      and 'ACCENT-CREW' in _p.stdout.decode())
_p = subprocess.run([sys.executable, MOD_PATH, '--gate', '--accent', 'teal',
                     '--bg', 'navy', '--crew', 'nope'], capture_output=True)
check('gate CLI unknown crew exit 2', _p.returncode == 2)

if FAILURES:
    print('FAILED: %d test(s): %s' % (len(FAILURES), ', '.join(FAILURES)))
    sys.exit(1)
print('all tests passed')
