#!/usr/bin/env python3
# test-design-tokens-cardparity.py -- DGN-376 T3 render-equivalence net.
#
# The card scripts (morning_brief_card.py, diet-log/card.py) had their
# hardcoded palette/font constants replaced by consumption of the token canon
# (routines/lib/design_tokens.py). The visual-regression guarantee is that
# every resolved constant is BYTE-IDENTICAL to the value it replaced -- the
# token Layer A was canonicalized FROM this very palette (T1), so a
# token-swap must produce pixel-identical cards. This suite freezes the
# former literals and asserts the token resolution still yields them.
#
# It does NOT import the card scripts (they pull matplotlib/lifekit); it
# resolves the token module directly and compares against the frozen literals.
# A separate live render diff (matplotlib present) is the deploy-time check;
# this value-parity net runs on any python3.
#
# Run: python3 routines/tests/test-design-tokens-cardparity.py
# Exit: 0 all pass, nonzero any fail. Python 3 stdlib only.

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.normpath(os.path.join(TESTS_DIR, '..', 'lib'))
MOD_PATH = os.path.join(LIB_DIR, 'design_tokens.py')

_spec = importlib.util.spec_from_file_location('design_tokens', MOD_PATH)
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)

T = dt.theme('card-dark')
B = dt.BRAND
FAILURES = []


def eq(name, got, want):
    if got == want:
        print('  ok  %s = %s' % (name, want))
    else:
        print('  FAIL %s: got %r want %r' % (name, got, want))
        FAILURES.append(name)


print('DGN-376 T3 card palette parity (frozen former literals vs token resolution)')

# --- morning_brief_card.py former palette block ---
print(' morning_brief_card.py')
eq('BG',         T['bg'],            '#13132B')
eq('INK',        T['text'],          '#FFFFFF')
eq('INK_SOFT',   T['muted'],         '#C4D4FF')
eq('INK_VALUE',  B['ink-value'],     '#DDDDEE')
eq('TRACK_BASE', B['navy-track'],    '#2A2A50')
eq('SEP_LINE',   B['navy-line'],     '#2A2A55')
eq('ACCENT',     T['accent'],        '#4ECDC4')
eq('WARM',       T['yellow'],        '#FFD166')
eq('QUOTE_BG',   B['navy-quote'],    '#1C1C3A')
eq('PANEL_BG',   T['surface'],       '#1A1A35')
eq('PANEL_EDGE', B['navy-line'],     '#2A2A55')
eq('AQ 좋음',     B['grade-good'],    '#2ECC71')
eq('AQ 보통',     B['grade-ok'],      '#A8D86E')
eq('AQ 나쁨',     B['grade-bad'],     '#95A5A6')
eq('AQ 매우나쁨', B['grade-vbad'],    '#636E72')

# --- diet-log/card.py former palette block ---
print(' diet-log/card.py')
eq('BG',          T['bg'],           '#13132B')
eq('INK',         T['text'],         '#FFFFFF')
eq('INK_SOFT',    T['muted'],        '#C4D4FF')
eq('INK_VALUE',   B['ink-value'],    '#DDDDEE')
eq('TRACK_BASE',  B['navy-track'],   '#2A2A50')
eq('BAR_EXERCISE_SEG', B['navy-seg'], '#1C1C38')
eq('BURN_ACCENT', T['orange'],       '#FF9F43')
eq('INTAKE_FILL', T['red'],          '#FF6B6B')
eq('SEP_LINE',    B['navy-line'],    '#2A2A55')
eq('MACRO 단백질',  T['accent'],       '#4ECDC4')
eq('MACRO 탄수화물', T['yellow'],       '#FFD166')
eq('MACRO 지방',    B['mint'],         '#A8E6CF')
eq('MEAL 아침',     T['red'],          '#FF6B6B')
eq('MEAL 점심',     T['accent'],       '#4ECDC4')
eq('MEAL 저녁',     B['mint'],         '#A8E6CF')
eq('MEAL 간식',     T['yellow'],       '#FFD166')
eq('MEAL 운동',     T['orange'],       '#FF9F43')

# --- font token names ---
print(' font tokens')
eq('FONT medium',    dt.FONTS['medium'],    'ASDGN_Medium.ttf')
eq('FONT extrabold', dt.FONTS['extrabold'], 'ASDGN_ExtraBold.ttf')

# --- font resolver contract (walk-up finds the bundle from card dirs) ---
print(' font resolver')
agent_root = os.path.normpath(os.path.join(TESTS_DIR, '..', '..'))
bundle_dir = os.path.join(
    agent_root, '.claude', 'skills-bundle', 'diet-log', 'fonts')
if os.path.isdir(bundle_dir):
    # From the brief-card location (routines/bundle/).
    med, xbld = dt.font_paths(os.path.join(agent_root, 'routines', 'bundle'))
    eq('brief-card resolves medium', os.path.basename(med), 'ASDGN_Medium.ttf')
    eq('brief-card resolves extrabold',
       os.path.basename(xbld), 'ASDGN_ExtraBold.ttf')
else:
    print('  skip  font bundle dir absent (%s)' % bundle_dir)

if FAILURES:
    print('FAILED: %d: %s' % (len(FAILURES), ', '.join(FAILURES)))
    sys.exit(1)
print('all parity checks passed')
