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
      set(dt.THEMES) == {'card-dark', 'console-dark', 'diagram'},
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

# 5. M5 invariant: console-dark is a deliberate alien register
#    (zero hex overlap with card-dark), anchored on GitHub-dark ground.
console = dt.THEMES['console-dark']
check('console-dark bg (measured views.py)', console['bg'] == '#0D1117')
check('card/console zero hex overlap',
      not (set(card.values()) & set(console.values())))

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
check('css bg line', '--bg: #0d1117;' in css)

# 9. JSON export round-trips and carries all three groups.
data = json.loads(dt.to_json())
check('json groups', set(data) == {'brand', 'fonts', 'themes'})
check('json theme parity', data['themes'] == dt.THEMES)

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

if FAILURES:
    print('FAILED: %d test(s): %s' % (len(FAILURES), ', '.join(FAILURES)))
    sys.exit(1)
print('all tests passed')
