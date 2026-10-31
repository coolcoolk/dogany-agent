#!/usr/bin/env python3
# test-lane: fast    # measured 0.1s (2026-09-04 full sweep, DGN-1267)
# test-onboarding-class-gate.py -- self-tests for the DGN-590 class gate in
# routines/onboarding-check.py. Replaces the retired tier gate (instance_tier /
# DOGANY_TIER) with instance_class / DOGANY_AGENT_CLASS.
#
# DGN-590 lock revised by DGN-1406 (owner approved 2026-09-12): a domain/staff
# instance is no longer "kitless by construction" -- it is kitless BY DEFAULT,
# but a DOGANY_KIT preset (install.sh / mint_run.sh) can seed it a real kit.
# The lifekit-walkthrough offer gate in onboarding-check.py dropped its
# instance_class == 'main' check accordingly (main() in onboarding-check.py).
#
# Verifies:
#   (1) instance_class() contract: 'domain' -> 'domain'; anything else /
#       missing field / missing file / exception -> 'main' (fail-open to main,
#       the DGN-590 gate direction reversal vs. the old fail-closed-to-lite).
#       UNCHANGED by DGN-1406 -- instance_class() itself still has this
#       contract; only its use as a lifekit-offer gate was removed.
#   (2) DGN-1406 invariant (a): with no DOGANY_KIT preset, KIT stays 'none' on
#       a domain (the anchor's job, exercised in tests/rehearsal_dgn227.sh) --
#       and kit_offer_pending('none', ...) is False regardless of class, so a
#       default-installed domain still never sees the lifekit-walkthrough
#       offer. Byte-identical to pre-DGN-1406.
#   (3) DGN-1406 invariant (b)+gate: once KIT names the life kit (as it would
#       after a DOGANY_KIT=lifekit preset) AND that kit's own LIFEKIT=pending,
#       the offer fires on EITHER class -- main or domain. This is the actual
#       behavior change: pre-DGN-1406 this fired on main only.
#
# Run: python3 routines/tests/test-onboarding-class-gate.py
# Exit: 0 all pass, nonzero any fail (CI-less self-test convention).
# Python 3 stdlib only.

import json
import os
import subprocess
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


# ---- (2) DGN-1406 unit gate: kit_offer_pending(KIT, data) is a pure function
#      of (KIT, LIFEKIT-state) -- class plays NO part any more. This locks
#      invariant (a) directly: KIT=none (the domain default with no preset)
#      never fires, on any LIFEKIT state -- byte-identical to pre-DGN-1406.
def test_gate_matrix():
    lifekits = {
        'pending': 'LIFEKIT=pending\n',
        'offered': 'LIFEKIT=offered\n',
        'on': 'LIFEKIT=on\n',
        'off': 'LIFEKIT=off\n',
        None: None,                                   # no file
    }
    with tempfile.TemporaryDirectory() as d:
        for kit in ('none', 'lifekit'):
            for lk, lbody in lifekits.items():
                lconf = os.path.join(d, f'l_{kit}_{lk}.conf')
                if lbody is not None:
                    write(lconf, lbody)
                os.environ['LIFEKIT_FILE'] = lconf
                # DGN-1817: the kit must also be installable; this matrix is
                # about the lifecycle table, so the probe answers yes here
                # (the no-source case is pinned below).
                saved = hook._life_engine_kit_installable
                hook._life_engine_kit_installable = lambda root: True
                try:
                    fires = hook.kit_offer_pending(kit, {})
                finally:
                    hook._life_engine_kit_installable = saved
                    del os.environ['LIFEKIT_FILE']
                # target table (DGN-1406): offer fires iff KIT names the life
                # kit AND its own lifecycle is pending -- no class term.
                expect = (kit == 'lifekit') and (lk == 'pending')
                check(f'kit_offer_pending[KIT={kit},lifekit={lk}]', bool(fires), expect)
        # DGN-1817 fail-closed: pending but neither installed nor
        # installable -> no offer.
        lconf = os.path.join(d, 'l_noinstall.conf')
        write(lconf, 'LIFEKIT=pending\n')
        os.environ['LIFEKIT_FILE'] = lconf
        saved = hook._life_engine_kit_installable
        hook._life_engine_kit_installable = lambda root: False
        try:
            fires = hook.kit_offer_pending('lifekit', {})
        finally:
            hook._life_engine_kit_installable = saved
            del os.environ['LIFEKIT_FILE']
        check('kit_offer_pending[KIT=lifekit,lifekit=pending,not installable]', bool(fires), False)


# ---- (3) DGN-1406 end-to-end: the real hook script, invoked as a subprocess
#      exactly as SessionStart would, must offer the lifekit walkthrough on a
#      domain/staff instance once KIT names the life kit (as a DOGANY_KIT
#      preset would seed it) -- the actual behavior change (pre-DGN-1406 this
#      required class=main). Runs the on-disk script, not the imported
#      module, so it also catches a regression in main()'s own gate wiring
#      (unit tests above only exercise the helper it calls).
def _mk_instance(root, klass, kit, lifekit_state):
    os.makedirs(os.path.join(root, 'identity'), exist_ok=True)
    os.makedirs(os.path.join(root, 'config'), exist_ok=True)
    # onboarding already complete -- no ONBOARDING_PENDING marker
    write(os.path.join(root, 'identity', 'hot.custom.agent.md'), '# AGENT\ndone.\n')
    write(os.path.join(root, 'config', 'agent.conf'), f'KIT={kit}\n')
    if lifekit_state is not None:
        write(os.path.join(root, 'config', 'lifekit.conf'), f'LIFEKIT={lifekit_state}\n')
    # DGN-1817: an installed kit (bundle.conf), so the offer is not
    # suppressed as uninstallable.
    os.makedirs(os.path.join(root, 'service', 'kit_life'), exist_ok=True)
    write(os.path.join(root, 'service', 'kit_life', 'bundle.conf'), '# fixture\n')
    write(os.path.join(root, '.instance.conf'), f'DOGANY_AGENT_CLASS={klass}\n')


def _run_hook(root):
    payload = json.dumps({'cwd': root, 'source': 'startup'})
    p = subprocess.run(
        [sys.executable, _HOOK_PATH], input=payload,
        capture_output=True, text=True, timeout=10)
    return p


def test_e2e_lifekit_offer_gate():
    with tempfile.TemporaryDirectory() as d:
        cases = [
            ('main',   'lifekit', 'pending', True),
            ('domain', 'lifekit', 'pending', True),   # DGN-1406: was False pre-revision
            ('domain', 'none',    'pending', False),  # invariant (a): default unaffected
            ('domain', 'lifekit', 'on',      False),  # kit chosen, walkthrough already done
        ]
        for i, (klass, kit, lk_state, expect_offer) in enumerate(cases):
            root = os.path.join(d, f'inst{i}')
            _mk_instance(root, klass, kit, lk_state)
            proc = _run_hook(root)
            label = f'e2e[class={klass},KIT={kit},LIFEKIT={lk_state}]'
            if proc.returncode != 0:
                _failures.append(f'{label}: hook exited {proc.returncode}, stderr={proc.stderr!r}')
                continue
            out = proc.stdout.strip()
            offered = False
            if out:
                try:
                    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                    offered = '[lifekit pending]' in ctx
                except Exception as e:
                    _failures.append(f'{label}: could not parse hook stdout ({e}): {out!r}')
                    continue
            check(label, offered, expect_offer)


if __name__ == '__main__':
    test_instance_class()
    test_gate_matrix()
    test_e2e_lifekit_offer_gate()
    if _failures:
        print("FAIL:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("PASS: onboarding class-gate matrix + instance_class contract + DGN-1406 e2e")
