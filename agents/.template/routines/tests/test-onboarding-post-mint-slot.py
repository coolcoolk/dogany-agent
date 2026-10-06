#!/usr/bin/env python3
# test-lane: fast
# test-onboarding-post-mint-slot.py -- self-tests for the DGN-1487 post-mint
# next-step slot in routines/onboarding-check.py.
#
# Contract under test (owner-confirmed 2-layer design, worklog/DGN-1487
# slot-interface lock 2026-09-15): after identity onboarding is done and no
# offer signal (kit-select / lifekit walkthrough) claimed the session, the
# hook resolves a post-mint slot value and injects it VERBATIM as the
# SessionStart context. The framework never parses the value.
#
#   resolution: config/agent.conf POST_MINT_NEXT
#               > config/packs/<pack_id>.post_mint_next (install-time record)
#               > unset (silent -- today's behavior)
#   empty (after strip) = unset at either layer (falls through)
#   any error anywhere  = silent, exit 0 (fail-open)
#
# Verifies (mapped from the DGN-1487 dispatch's mandatory list):
#   (1) domain-shaped instance, identity done + slot declared -> the slot
#       value surfaces verbatim at SessionStart (the domain stage wiring).
#   (2) slot unset -> zero output, exit 0 (silence stays the default).
#   (3) identity onboarding NOT done + slot declared -> identity signal only,
#       slot never surfaces (ordering preserved).
#   (4) non-domain shape (leader/dev instances: no slot anywhere) -> zero output,
#       exit 0, and the pre-existing offer signals are untouched (regression).
#   (5) declared-but-broken shapes (record unreadable, config/packs is a
#       file, empty record) -> no-op, no error, no crash (fail-open).
#   (6) precedence: an offer signal (KIT=pending) beats the slot -- at most
#       ONE signal per session.
#   (7) empty conf value falls through to the pack record (empty != disable).
#
# DGN-1632 instruction-contract checks (added below, not proof of model
# behavior): the direct-mint identity completion procedure must prescribe
# the one inline selector call, migration exclusion, silence-on-empty/
# malformed/failed output, and same-turn continuation, on ALL THREE
# instruction surfaces (both ONBOARDING_CTX languages here, plus the
# template SKILL.md and identity/hot.custom.agent.md onboarding block) --
# and none of the three may retain the old unconditional direct-mint
# soft-invite close.
#
# Run: python3 routines/tests/test-onboarding-post-mint-slot.py
# Exit: 0 all pass, nonzero any fail (CI-less self-test convention).
# Python 3 stdlib only.

import json
import os
import stat
import subprocess
import sys
import tempfile
import importlib.util

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTINES_DIR = os.path.normpath(os.path.join(TESTS_DIR, '..'))
_HOOK_PATH = os.environ.get(
    'ONBOARDING_HOOK_UNDER_TEST',
    os.path.join(ROUTINES_DIR, 'onboarding-check.py'))

_spec = importlib.util.spec_from_file_location('onboarding_check', _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

_failures = []

SLOT_VALUE = ('[domain onboarding pending] identity onboarding is done but '
              'the domain stage has not run. Run the health-onboarding skill '
              'to start the goal consultation (ENTRY: health-onboarding).')


def check(name, got, want):
    if got != want:
        _failures.append(f"{name}: got {got!r}, want {want!r}")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def mk_instance(root, klass='domain', kit='none', marker=False,
                conf_extra='', pack_records=None):
    """Build a minimal instance fixture. pack_records: {filename: content}
    dropped into config/packs/."""
    os.makedirs(os.path.join(root, 'identity'), exist_ok=True)
    os.makedirs(os.path.join(root, 'config'), exist_ok=True)
    body = '# AGENT\n'
    if marker:
        body += '<!-- ONBOARDING_PENDING -->\n'
    body += 'persona.\n'
    write(os.path.join(root, 'identity', 'hot.custom.agent.md'), body)
    write(os.path.join(root, 'config', 'agent.conf'),
          f'KIT={kit}\n{conf_extra}')
    if klass is not None:
        write(os.path.join(root, '.instance.conf'),
              f'DOGANY_AGENT_CLASS={klass}\n')
    for name, content in (pack_records or {}).items():
        write(os.path.join(root, 'config', 'packs', name), content)
    # DGN-1817: the life kit is offered only when installed or installable;
    # main fixtures carry an installed kit (bundle.conf) so the offer
    # signals under test are not suppressed as uninstallable.
    if klass == 'main':
        write(os.path.join(root, 'service', 'kit_life', 'bundle.conf'), '# fixture\n')


def run_hook(root, source='startup'):
    payload = json.dumps({'cwd': root, 'source': source})
    env = dict(os.environ)
    # Isolate from any ambient overrides a dev shell might carry.
    for k in ('ONBOARDING_FILE', 'AGENT_CONF_FILE', 'INSTANCE_CONF_FILE',
              'LIFEKIT_FILE'):
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, _HOOK_PATH], input=payload,
        capture_output=True, text=True, timeout=10, env=env)


def ctx_of(proc):
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


# ---- unit: resolver contract ------------------------------------------------
def test_resolver_unit():
    if not hasattr(hook, 'resolve_post_mint_next'):
        # keep going so a mutation/revert run still reports the e2e cases
        _failures.append('unit[*]: hook has no resolve_post_mint_next '
                         '(pre-DGN-1487 hook under test)')
        return
    with tempfile.TemporaryDirectory() as d:
        # conf override wins over record
        mk_instance(d, conf_extra='POST_MINT_NEXT=conf-wins\n',
                    pack_records={'pack_x.post_mint_next': 'record-value\n'})
        check('unit[conf_wins]',
              hook.resolve_post_mint_next({'cwd': d}), 'conf-wins')
    with tempfile.TemporaryDirectory() as d:
        # empty conf value falls through to record (empty = unset clause)
        mk_instance(d, conf_extra='POST_MINT_NEXT=\n',
                    pack_records={'pack_x.post_mint_next': 'record-value\n'})
        check('unit[empty_conf_falls_to_record]',
              hook.resolve_post_mint_next({'cwd': d}), 'record-value')
    with tempfile.TemporaryDirectory() as d:
        # record only
        mk_instance(d, pack_records={'pack_x.post_mint_next': 'record-value\n'})
        check('unit[record_only]',
              hook.resolve_post_mint_next({'cwd': d}), 'record-value')
    with tempfile.TemporaryDirectory() as d:
        # whitespace-only record = unset
        mk_instance(d, pack_records={'pack_x.post_mint_next': '  \n\n'})
        check('unit[blank_record_unset]',
              hook.resolve_post_mint_next({'cwd': d}), None)
    with tempfile.TemporaryDirectory() as d:
        # nothing declared anywhere
        mk_instance(d)
        check('unit[nothing_declared]',
              hook.resolve_post_mint_next({'cwd': d}), None)
    with tempfile.TemporaryDirectory() as d:
        # deterministic tiebreak: sorted(name), first non-empty wins;
        # empty earlier record is skipped, non-suffix files ignored
        mk_instance(d, pack_records={
            'a_pack.post_mint_next': '\n',
            'b_pack.post_mint_next': 'b-wins\n',
            'c_pack.post_mint_next': 'c-loses\n',
            'kit_life.requires_framework': '>=1.30.0\n',
        })
        check('unit[sorted_first_nonempty]',
              hook.resolve_post_mint_next({'cwd': d}), 'b-wins')
    with tempfile.TemporaryDirectory() as d:
        # config/packs is a FILE, not a dir -> fail-open None
        mk_instance(d)
        write(os.path.join(d, 'config', 'packs'), 'not a dir')
        check('unit[packs_is_file]',
              hook.resolve_post_mint_next({'cwd': d}), None)
    if os.geteuid() != 0:
        with tempfile.TemporaryDirectory() as d:
            # unreadable record file -> skipped, next one wins (fail-open)
            mk_instance(d, pack_records={
                'a_pack.post_mint_next': 'unreachable\n',
                'b_pack.post_mint_next': 'fallback\n'})
            locked = os.path.join(d, 'config', 'packs', 'a_pack.post_mint_next')
            os.chmod(locked, 0)
            try:
                check('unit[unreadable_record_skipped]',
                      hook.resolve_post_mint_next({'cwd': d}), 'fallback')
            finally:
                os.chmod(locked, stat.S_IRUSR | stat.S_IWUSR)


# ---- (1) e2e: domain stage surfaces, verbatim -------------------------------
def test_e2e_slot_fires_verbatim():
    for label, kw in (
        ('record', dict(pack_records={'pack_health.post_mint_next':
                                      SLOT_VALUE + '\n'})),
        ('conf', dict(conf_extra=f'POST_MINT_NEXT={SLOT_VALUE}\n')),
        ('empty_conf_plus_record',
         dict(conf_extra='POST_MINT_NEXT=\n',
              pack_records={'pack_health.post_mint_next': SLOT_VALUE + '\n'})),
    ):
        with tempfile.TemporaryDirectory() as d:
            mk_instance(d, klass='domain', kit='none', **kw)
            proc = run_hook(d)
            check(f'e2e_fires[{label}].rc', proc.returncode, 0)
            check(f'e2e_fires[{label}].ctx', ctx_of(proc), SLOT_VALUE)


# ---- (2) e2e: slot unset -> silent ------------------------------------------
def test_e2e_unset_silent():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, klass='domain', kit='none')
        proc = run_hook(d)
        check('e2e_unset.rc', proc.returncode, 0)
        check('e2e_unset.stdout', proc.stdout, '')


# ---- (3) e2e: identity onboarding not done -> identity signal only ----------
def test_e2e_identity_stage_first():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, klass='domain', kit='none', marker=True,
                    pack_records={'pack_health.post_mint_next':
                                  SLOT_VALUE + '\n'})
        proc = run_hook(d)
        check('e2e_identity_first.rc', proc.returncode, 0)
        ctx = ctx_of(proc)
        # fixture conf carries no AGENT_LANG -> en default -> en marker
        check('e2e_identity_first.is_onboarding',
              bool(ctx) and '[onboarding needed]' in ctx, True)
        check('e2e_identity_first.slot_absent',
              bool(ctx) and (SLOT_VALUE in ctx), False)


# ---- (4) e2e: non-domain regression -----------------------------------------
def test_e2e_non_domain_regression():
    # leader/dev-instance shape: no slot declared anywhere -> byte-for-byte silence.
    shapes = [
        ('main_explicit', dict(klass='main', kit='none')),
        ('no_instance_conf', dict(klass=None, kit='none')),
        ('domain_default', dict(klass='domain', kit='none')),
    ]
    for label, kw in shapes:
        with tempfile.TemporaryDirectory() as d:
            mk_instance(d, **kw)
            proc = run_hook(d)
            check(f'e2e_regression[{label}].rc', proc.returncode, 0)
            check(f'e2e_regression[{label}].stdout', proc.stdout, '')
    # pre-existing offer signals still fire, untouched by the slot branch
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, klass='main', kit='pending')
        ctx = ctx_of(run_hook(d))
        check('e2e_regression[kit_select_alive]',
              bool(ctx) and '[kit selection pending]' in ctx, True)
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, klass='main', kit='lifekit')
        write(os.path.join(d, 'config', 'lifekit.conf'), 'LIFEKIT=pending\n')
        ctx = ctx_of(run_hook(d))
        check('e2e_regression[lifekit_alive]',
              bool(ctx) and '[lifekit pending]' in ctx, True)
    # non-SessionStart sources stay silent even with a slot declared
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, klass='domain',
                    pack_records={'pack_health.post_mint_next':
                                  SLOT_VALUE + '\n'})
        proc = run_hook(d, source='resume')
        check('e2e_regression[non_startup_source].stdout', proc.stdout, '')
        check('e2e_regression[non_startup_source].rc', proc.returncode, 0)


# ---- (5) e2e: broken shapes are silent no-ops -------------------------------
def test_e2e_broken_shapes_silent():
    with tempfile.TemporaryDirectory() as d:
        # config/packs is a file
        mk_instance(d, klass='domain')
        write(os.path.join(d, 'config', 'packs'), 'not a dir')
        proc = run_hook(d)
        check('e2e_broken[packs_is_file].rc', proc.returncode, 0)
        check('e2e_broken[packs_is_file].stdout', proc.stdout, '')
    with tempfile.TemporaryDirectory() as d:
        # empty record only
        mk_instance(d, klass='domain',
                    pack_records={'pack_health.post_mint_next': '\n'})
        proc = run_hook(d)
        check('e2e_broken[empty_record].rc', proc.returncode, 0)
        check('e2e_broken[empty_record].stdout', proc.stdout, '')


# ---- (6) e2e: offer signals outrank the slot (one signal per session) -------
def test_e2e_precedence_one_signal():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, klass='main', kit='pending',
                    pack_records={'pack_health.post_mint_next':
                                  SLOT_VALUE + '\n'})
        proc = run_hook(d)
        ctx = ctx_of(proc)
        check('e2e_precedence.kit_select_fires',
              bool(ctx) and '[kit selection pending]' in ctx, True)
        check('e2e_precedence.slot_suppressed',
              bool(ctx) and SLOT_VALUE in ctx, False)


# ---- DGN-1632 instruction-contract checks -----------------------------------
# String-level assertions only: these prove the three instruction surfaces
# PRESCRIBE the right procedure, never that a model actually follows it (the
# acceptance replay covers that, separately and outside this suite).

def _resolve_template_dir():
    """The unminted template tree, or None.

    DOGANY_TEMPLATE_DIR wins when it names a directory. Otherwise the tree
    this file ships in (the parent of routines/) counts as the template only
    when it is NOT a live instance (no .instance.conf): the framework update
    copies routines/tests/ into instances, where that parent is the instance
    root and its persona is already onboarded (DGN-1734)."""
    env = os.environ.get('DOGANY_TEMPLATE_DIR', '')
    if env:
        return os.path.abspath(env) if os.path.isdir(env) else None
    here = os.path.normpath(os.path.join(ROUTINES_DIR, '..'))
    if os.path.exists(os.path.join(here, '.instance.conf')):
        return None
    return here


TEMPLATE_DIR = _resolve_template_dir()
_SKILL_PATH = _PERSONA_PATH = None
if TEMPLATE_DIR:
    _SKILL_PATH = os.path.join(
        TEMPLATE_DIR, '.claude', 'skills', 'dogany-user-onboarding',
        'SKILL.md')
    _PERSONA_PATH = os.path.join(
        TEMPLATE_DIR, 'identity', 'hot.custom.agent.md')

# Old unconditional direct-mint soft-invite closes (DGN-1632 deletes these).
_OLD_SOFT_INVITE_STRINGS = (
    'Feel free to ask me anything.',
    '언제든지 말씀해 주세요.',
)

# Markers the new procedure must carry, on all three surfaces (both
# ONBOARDING_CTX languages count as two of the three -- worklog DGN-1632
# "three instruction surfaces").
_NEW_PROCEDURE_MARKERS_EN = (
    'POST-IDENTITY CONTINUATION CHECK (DGN-1632)',
    'python3 <own-root>/routines/onboarding-check.py',
    'ONBOARDING_FILE',
    'LIFEKIT_FILE',
    'NONEMPTY STDOUT',
    'NO CONTINUATION',
    'At most ONE setup offer',
    'excluded entirely from the migration',
)
_NEW_PROCEDURE_MARKERS_KO = (
    '정체성 완료 후 연속성 점검 (DGN-1632)',
    'python3 <own-root>/routines/onboarding-check.py',
    'ONBOARDING_FILE',
    'LIFEKIT_FILE',
    'NONEMPTY STDOUT',
    '연속성 없음',
    '설정 오퍼는 최대 한 개',
    '그 경로는 자신의 마무리를 그대로 유지한다',
)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _template_contract_available():
    """The instruction contract belongs to the unminted template only."""
    if (TEMPLATE_DIR and os.path.isfile(_SKILL_PATH)
            and os.path.isfile(_PERSONA_PATH)):
        return True
    print('SKIP DGN-1632/DGN-1734: template onboarding contract unavailable '
          '(no template tree: live instance without DOGANY_TEMPLATE_DIR) '
          '-- NOT a pass')
    return False


def test_contract_onboarding_ctx_both_languages():
    """The hook's own ONBOARDING_CTX en/ko values (source of truth for the
    injected SessionStart context) must carry the new inline-selector
    procedure and drop the old unconditional soft invite."""
    if not _template_contract_available():
        return
    for lang, markers in (('en', _NEW_PROCEDURE_MARKERS_EN),
                          ('ko', _NEW_PROCEDURE_MARKERS_KO)):
        ctx = hook.ONBOARDING_CTX.get(lang, '')
        check(f'contract[hook.{lang}].nonempty', bool(ctx), True)
        for marker in markers:
            check(f'contract[hook.{lang}].has[{marker[:40]!r}]',
                  marker in ctx, True)
        for old in _OLD_SOFT_INVITE_STRINGS:
            check(f'contract[hook.{lang}].no_old_soft_invite[{old!r}]',
                  old in ctx, False)


def test_contract_skill_md():
    """The dogany-user-onboarding SKILL.md direct-mint close must match the
    hook's procedure semantically (same markers), not retain the old close."""
    if not _template_contract_available():
        return
    text = _read(_SKILL_PATH)
    for marker in _NEW_PROCEDURE_MARKERS_EN:
        check(f'contract[SKILL.md].has[{marker[:40]!r}]', marker in text, True)
    for old in _OLD_SOFT_INVITE_STRINGS:
        check(f'contract[SKILL.md].no_old_soft_invite[{old!r}]',
              old in text, False)


def test_contract_persona_onboarding_block():
    """The identity/hot.custom.agent.md onboarding block's direct-mint close
    must match the hook's procedure semantically, not retain the old close."""
    if not _template_contract_available():
        return
    text = _read(_PERSONA_PATH)
    for marker in _NEW_PROCEDURE_MARKERS_EN:
        check(f'contract[persona].has[{marker[:40]!r}]', marker in text, True)
    for old in _OLD_SOFT_INVITE_STRINGS:
        check(f'contract[persona].no_old_soft_invite[{old!r}]',
              old in text, False)


def test_contract_migration_excluded():
    """Migration/handoff completion keeps its own ending -- the continuation
    check must never claim to apply there. All three surfaces plus both
    ONBOARDING_CTX languages state the exclusion explicitly."""
    if not _template_contract_available():
        return
    en_ctx = hook.ONBOARDING_CTX.get('en', '')
    ko_ctx = hook.ONBOARDING_CTX.get('ko', '')
    check('contract[hook.en].migration_excluded',
          'excluded entirely from the migration' in en_ctx, True)
    check('contract[hook.ko].migration_excluded',
          '위 마이그레이션 경로에는 전혀 적용되지 않으며' in ko_ctx, True)
    check('contract[SKILL.md].migration_excluded',
          'excluded entirely from the migration' in _read(_SKILL_PATH), True)
    check('contract[persona].migration_excluded',
          'excluded entirely from the migration' in _read(_PERSONA_PATH), True)


if __name__ == '__main__':
    test_resolver_unit()
    test_e2e_slot_fires_verbatim()
    test_e2e_unset_silent()
    test_e2e_identity_stage_first()
    test_e2e_non_domain_regression()
    test_e2e_broken_shapes_silent()
    test_e2e_precedence_one_signal()
    test_contract_onboarding_ctx_both_languages()
    test_contract_skill_md()
    test_contract_persona_onboarding_block()
    test_contract_migration_excluded()
    if _failures:
        print("FAIL:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("PASS: DGN-1487 post-mint slot -- resolver contract + e2e "
          "(fires/silent/ordering/regression/fail-open/precedence)")
