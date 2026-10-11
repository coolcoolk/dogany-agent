#!/usr/bin/env python3
# test-lane: fast
# test-dgn1425-font-resolver.py -- DGN-1425 regression net.
#
# design_tokens.find_font_dir() hardcoded .claude/skills-bundle/diet-log/fonts
# with no fallback, so retiring diet-log (kit_life 2.19.0) made font_paths()
# return nothing even when the faces were present elsewhere. Resolution is now
# by declaration (a dir holding FONTS["medium"]), any skill bundle, then system
# font dirs, and ('', '') is the normal no-font path.
#
# Run: python3 routines/tests/test-dgn1425-font-resolver.py
# Exit: 0 all pass, nonzero any fail. Python 3 stdlib only.

import importlib.util
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MOD_PATH = os.path.normpath(
    os.path.join(TESTS_DIR, '..', 'lib', 'design_tokens.py'))

_spec = importlib.util.spec_from_file_location('design_tokens', MOD_PATH)
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)

FAILURES = []


def check(name, cond):
    print('  %s  %s' % ('ok  ' if cond else 'FAIL', name))
    if not cond:
        FAILURES.append(name)


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').close()


for k in ('AGENT_ROOT', 'DOGANY_FONT_DIR'):
    os.environ.pop(k, None)

with tempfile.TemporaryDirectory() as tmp:
    sysdir = os.path.join(tmp, 'sysfonts')
    os.makedirs(sysdir)
    dt._SYSTEM_FONT_DIRS = (sysdir,)

    # 1. diet-log absent, faces in another skill bundle -> found (the defect).
    root = os.path.join(tmp, 'agent')
    other = os.path.join(root, '.claude', 'skills-bundle', 'diet-status',
                         'fonts')
    touch(os.path.join(other, dt.FONTS['medium']))
    touch(os.path.join(other, dt.FONTS['extrabold']))
    start = os.path.join(root, 'routines', 'bundle')
    os.makedirs(start)
    check('no diet-log: walk-up finds other bundle',
          dt.find_font_dir(start) == other)
    med, xbld = dt.font_paths(start)
    check('font_paths medium', med == os.path.join(other, 'ASDGN_Medium.ttf'))
    check('font_paths extrabold',
          xbld == os.path.join(other, 'ASDGN_ExtraBold.ttf'))

    # 2. AGENT_ROOT path uses the same bundle search.
    os.environ['AGENT_ROOT'] = root
    check('AGENT_ROOT finds other bundle',
          dt.find_font_dir(os.path.join(tmp, 'elsewhere')) == other)
    del os.environ['AGENT_ROOT']

    # 3. A bundle fonts/ dir without the faces is not a font home.
    empty_root = os.path.join(tmp, 'empty')
    os.makedirs(os.path.join(empty_root, '.claude', 'skills-bundle',
                             'diet-log', 'fonts'))
    estart = os.path.join(empty_root, 'routines')
    os.makedirs(estart)
    check('empty fonts dir + no system fonts -> None',
          dt.find_font_dir(estart) is None)
    check('font_paths -> ("", "") without raising',
          dt.font_paths(estart) == ('', ''))

    # 4. System font fallback (one level deep), medium-only -> extrabold ''.
    touch(os.path.join(sysdir, 'asdgn', dt.FONTS['medium']))
    check('system fallback found',
          dt.find_font_dir(estart) == os.path.join(sysdir, 'asdgn'))
    med, xbld = dt.font_paths(estart)
    check('system medium path', os.path.basename(med) == 'ASDGN_Medium.ttf')
    check('missing extrabold -> ""', xbld == '')

    # 5. Explicit declaration wins.
    decl = os.path.join(tmp, 'declared')
    touch(os.path.join(decl, dt.FONTS['medium']))
    os.environ['DOGANY_FONT_DIR'] = decl
    check('DOGANY_FONT_DIR wins', dt.find_font_dir(start) == decl)
    del os.environ['DOGANY_FONT_DIR']

# 6. No retired skill name in the resolver's lookup path.
check('no diet-log path constant',
      not hasattr(dt, '_FONT_BUNDLE_REL'))

if FAILURES:
    print('FAILED: %d: %s' % (len(FAILURES), ', '.join(FAILURES)))
    sys.exit(1)
print('all DGN-1425 font resolver checks passed')
