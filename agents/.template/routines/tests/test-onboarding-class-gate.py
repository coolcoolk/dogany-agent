#!/usr/bin/env python3
# test-onboarding-class-gate.py -- self-tests for the DGN-590 class gate in
# routines/onboarding-check.py. Replaces the retired tier gate (instance_tier /
# DOGANY_TIER) with instance_class / DOGANY_AGENT_CLASS.
#
# Verifies:
#   (1) instance_class() contract: 'domain' -> 'domain'; anything else /
#       missing field / missing file / exception -> 'main' (fail-open to main,
#       the DGN-590 gate direction reversal vs. the old fail-closed-to-lite).
#   (2) the lifekit-offer gate matrix {class: main/domain/absent/no-file} x
#       {LIFEKIT: pending/offered/on/off/no-file} matches the target table:
#       an offer signal fires ONLY when class resolves to main AND LIFEKIT is
#       pending.
#
# Run: python3 routines/tests/test-onboarding-class-gate.py
# Exit: 0 all pass, nonzero any fail (CI-less self-test convention).
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


def write(path, content):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


# ---- (1) instance_class() contract -----------------------------------------
def test_instance_class():
    with tempfile.TemporaryDirectory() as d:
        cases = {
            'domain': 'DOGANY_AGENT_CLASS=domain\n',
            'main': 'DOGANY_AGENT_CLASS=main\n',
            'poc_val_to_main': 'DOGANY_AGENT_CLASS=poc\n',
            'empty_val_to_main': 'DOGANY_AGENT_CLASS=\n',
            'field_absent_to_main': 'DOGANY_MINTED_AT=2026-07-27\n',
        }
        want = {
            'domain': 'domain',
            'main': 'main',
            'poc_val_to_main': 'main',
            'empty_val_to_main': 'main',
            'field_absent_to_main': 'main',
        }
        for key, body in cases.items():
            p = os.path.join(d, f'{key}.conf')
            write(p, body)
            check(f'instance_class[{key}]', hook.instance_class(p), want[key])
        # missing file -> main
        check('instance_class[missing_file]',
              hook.instance_class(os.path.join(d, 'nope.conf')), 'main')
        # case-insensitive DOMAIN
        p = os.path.join(d, 'upper.conf')
        write(p, 'DOGANY_AGENT_CLASS=DOMAIN\n')
        check('instance_class[upper_domain]', hook.instance_class(p), 'domain')


# ---- (2) gate matrix: offer fires iff class==main AND LIFEKIT==pending ------
def test_gate_matrix():
    classes = {
        'main': 'DOGANY_AGENT_CLASS=main\n',
        'domain': 'DOGANY_AGENT_CLASS=domain\n',
        'absent': 'DOGANY_MINTED_AT=2026-07-27\n',   # field absent -> main
        None: None,                                   # no file -> main
    }
    lifekits = {
        'pending': 'LIFEKIT=pending\n',
        'offered': 'LIFEKIT=offered\n',
        'on': 'LIFEKIT=on\n',
        'off': 'LIFEKIT=off\n',
        None: None,                                   # no file
    }
    with tempfile.TemporaryDirectory() as d:
        for ck, cbody in classes.items():
            iconf = os.path.join(d, f'i_{ck}.conf')
            if cbody is not None:
                write(iconf, cbody)
            resolved_class = hook.instance_class(iconf)
            for lk, lbody in lifekits.items():
                lconf = os.path.join(d, f'l_{ck}_{lk}.conf')
                if lbody is not None:
                    write(lconf, lbody)
                pending = hook.lifekit_pending(lconf)
                fires = pending and resolved_class == 'main'
                # target table: offer only when class main AND lifekit pending
                expect = (ck in ('main', 'absent', None)) and (lk == 'pending')
                check(f'gate[class={ck},lifekit={lk}]', bool(fires), expect)


if __name__ == '__main__':
    test_instance_class()
    test_gate_matrix()
    if _failures:
        print("FAIL:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("PASS: onboarding class-gate matrix + instance_class contract")
