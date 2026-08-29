#!/usr/bin/env python3
# test-onboarding-persona-dual-accept.py -- DGN-773 T3 self-tests for the
# AGENT.md -> PROFILE.md dual-accept resolver in routines/onboarding-check.py.
#
# Covers the 4-quadrant matrix required by the T3 spec, at the two call
# points that changed:
#   (1) resolve_persona_md(root)  -- the core dual-accept resolver
#       [a] PROFILE.md only        -> resolves PROFILE.md, no collision
#       [b] AGENT.md only          -> resolves AGENT.md, no collision
#       [c] both, AGENT.md a REAL file -> resolves PROFILE.md, collision=True
#       [d] neither                -> (None, False)  (fail-safe: caller must
#           treat this as onboarding-needed, never as "already done")
#   (2) symlink case: PROFILE.md + AGENT.md as a symlink TO PROFILE.md (the
#       normal post-2.0-rename compat shape, DGN-773 R7) must NOT be reported
#       as a collision -- this is the anti-false-positive leg of quadrant [c].
#   (3) resolve_target(data) -- ONBOARDING_FILE env wiring:
#       - unset -> dual-accept resolves against stdin cwd
#       - set to a directory (2.0+ settings.json wiring) -> dual-accept root
#       - set to a literal file path (legacy override) -> honored verbatim,
#         no dual-accept, collision always False
#   (4) needs_onboarding(path) fail-safe direction: None/missing -> True
#       (onboarding fires) which is the safe misfire direction per the spec
#       (a wrong "needs onboarding" is merely an extra question; a wrong
#       "no onboarding needed" would silently strand a fresh instance).
#
# Run: python3 routines/tests/test-onboarding-persona-dual-accept.py
# Exit: 0 all pass, nonzero any fail (CI-less self-test convention, same
# harness shape as test-onboarding-class-gate.py in this directory).
# Python 3 stdlib only.

import os
import sys
import tempfile
import importlib.util

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTINES_DIR = os.path.normpath(os.path.join(TESTS_DIR, '..'))
_HOOK_PATH = os.path.join(ROUTINES_DIR, 'onboarding-check.py')

_spec = importlib.util.spec_from_file_location('onboarding_check', _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append(f"{name}: got {got!r}, want {want!r}")


def write(path, content="stub\n"):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


# ---- (1) resolve_persona_md(root) -- 4-quadrant matrix ---------------------
def test_resolve_persona_md_quadrants():
    with tempfile.TemporaryDirectory() as d:
        # [a] PROFILE.md only
        a = os.path.join(d, 'a')
        os.makedirs(a)
        write(os.path.join(a, 'PROFILE.md'))
        path, collision = hook.resolve_persona_md(a)
        check('[a] PROFILE-only path', path, os.path.join(a, 'PROFILE.md'))
        check('[a] PROFILE-only collision', collision, False)

        # [b] AGENT.md only (pre-2.0 shape, current live default)
        b = os.path.join(d, 'b')
        os.makedirs(b)
        write(os.path.join(b, 'AGENT.md'))
        path, collision = hook.resolve_persona_md(b)
        check('[b] AGENT-only path', path, os.path.join(b, 'AGENT.md'))
        check('[b] AGENT-only collision', collision, False)

        # [c] both exist as INDEPENDENT REAL FILES -> mid-migration collision.
        # PROFILE.md must still win the resolution.
        c = os.path.join(d, 'c')
        os.makedirs(c)
        write(os.path.join(c, 'PROFILE.md'), 'profile content\n')
        write(os.path.join(c, 'AGENT.md'), 'agent content\n')
        path, collision = hook.resolve_persona_md(c)
        check('[c] both-real-files path (PROFILE wins)', path, os.path.join(c, 'PROFILE.md'))
        check('[c] both-real-files collision flagged', collision, True)

        # [d] neither exists -> None, no collision. Caller (needs_onboarding)
        # must treat None as onboarding-needed (fail-safe direction, see (4)).
        dd = os.path.join(d, 'd')
        os.makedirs(dd)
        path, collision = hook.resolve_persona_md(dd)
        check('[d] neither-exists path', path, None)
        check('[d] neither-exists collision', collision, False)


# ---- (2) symlink case -- must NOT be flagged as a collision -----------------
def test_symlink_not_collision():
    with tempfile.TemporaryDirectory() as d:
        write(os.path.join(d, 'PROFILE.md'), 'profile content\n')
        # Normal post-2.0-rename compat shape (DGN-773 R7): AGENT.md is a
        # symlink TO PROFILE.md, not an independent real file.
        os.symlink(os.path.join(d, 'PROFILE.md'), os.path.join(d, 'AGENT.md'))
        path, collision = hook.resolve_persona_md(d)
        check('symlink-shape path (PROFILE wins)', path, os.path.join(d, 'PROFILE.md'))
        check('symlink-shape must NOT warn (not a real collision)', collision, False)


# ---- (3) resolve_target(data) -- ONBOARDING_FILE env wiring ----------------
def test_resolve_target_env_wiring():
    old = os.environ.get('ONBOARDING_FILE')
    try:
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, 'PROFILE.md'))

            # unset -> falls back to stdin cwd (dual-accept)
            os.environ.pop('ONBOARDING_FILE', None)
            path, collision = hook.resolve_target({'cwd': d})
            check('resolve_target unset-env path', path, os.path.join(d, 'PROFILE.md'))
            check('resolve_target unset-env collision', collision, False)

            # set to a DIRECTORY (2.0+ settings.json wiring) -> dual-accept root
            os.environ['ONBOARDING_FILE'] = d
            path, collision = hook.resolve_target({'cwd': '/should/not/be/used'})
            check('resolve_target dir-env path', path, os.path.join(d, 'PROFILE.md'))
            check('resolve_target dir-env collision', collision, False)

            # set to a LITERAL FILE PATH (legacy explicit override) -> honored
            # verbatim, no dual-accept, no collision check even if the dir
            # actually has a collision.
            write(os.path.join(d, 'AGENT.md'), 'agent content\n')
            legacy_file = os.path.join(d, 'AGENT.md')
            os.environ['ONBOARDING_FILE'] = legacy_file
            path, collision = hook.resolve_target({'cwd': d})
            check('resolve_target literal-file-env path (legacy honored verbatim)',
                  path, legacy_file)
            check('resolve_target literal-file-env never collision-checks',
                  collision, False)
    finally:
        if old is None:
            os.environ.pop('ONBOARDING_FILE', None)
        else:
            os.environ['ONBOARDING_FILE'] = old


# ---- (4) needs_onboarding fail-safe direction -------------------------------
def test_needs_onboarding_failsafe():
    with tempfile.TemporaryDirectory() as d:
        # None (neither PROFILE.md nor AGENT.md) -> True (onboarding fires).
        # This is the deliberate fail-safe direction: misfiring "needs
        # onboarding" is a re-askable extra question; misfiring "no
        # onboarding needed" on a genuinely fresh/unminted instance would
        # silently strand it with no identity ever filled in.
        check('needs_onboarding(None) -> True (fail-safe)',
              hook.needs_onboarding(None), True)

        # PROFILE.md present, no marker -> False (onboarding NOT re-triggered
        # by dual-accept alone -- only the ONBOARDING_PENDING marker fires it).
        p = os.path.join(d, 'PROFILE.md')
        write(p, 'no marker here\n')
        check('needs_onboarding(PROFILE.md, no marker) -> False',
              hook.needs_onboarding(p), False)

        # AGENT.md present with the marker -> True.
        a = os.path.join(d, 'AGENT.md')
        write(a, hook.MARKER + '\nrest of file\n')
        check('needs_onboarding(AGENT.md, marker present) -> True',
              hook.needs_onboarding(a), True)


if __name__ == '__main__':
    test_resolve_persona_md_quadrants()
    test_symlink_not_collision()
    test_resolve_target_env_wiring()
    test_needs_onboarding_failsafe()
    if _failures:
        print("FAIL:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("PASS: onboarding-check persona dual-accept (DGN-773 T3)")
