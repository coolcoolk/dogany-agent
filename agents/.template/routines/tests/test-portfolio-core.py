#!/usr/bin/env python3
# test-portfolio-core.py -- self-tests for the portfolio core lint
# (routines/lib/portfolio-core-lint.py) and the generic parse entrypoint
# (routines/lib/portfolio-core-parse.sh). Spec of record: docs/PORTFOLIO-CORE.md.
#
# Origin: DGN-350 executable-gate suite, graduated with fixtures
# de-personalized and every machine-local dependency removed -- this suite
# passes on any machine with python3 + bash (no instance workspace assumed).
#
# Run: python3 routines/tests/test-portfolio-core.py
# Exit: 0 all pass, nonzero any fail (CI-less self-test convention, same as
# test-cron-guard.sh). Release discipline: run this suite as part of the
# release-preflight pass.
# Python 3 stdlib only.

import sys
import os
import json
import tempfile
import collections
import importlib.util

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.normpath(os.path.join(TESTS_DIR, '..', 'lib'))
FIXTURES = os.path.join(TESTS_DIR, 'fixtures', 'portfolio')

# Import the lint module from its dashed filename (not a package).
_LINT_PATH = os.path.join(LIB_DIR, 'portfolio-core-lint.py')
_spec = importlib.util.spec_from_file_location('portfolio_core_lint', _LINT_PATH)
lint_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_mod)

PORTFOLIO_C0   = os.path.join(FIXTURES, 'portfolio_c0_legacy.md')
PORTFOLIO_FULL = os.path.join(FIXTURES, 'portfolio_full.md')
MANIFEST_BASE  = os.path.join(FIXTURES, 'manifest_min.md')
MANIFEST_TOMB  = os.path.join(FIXTURES, 'manifest_min_mutant_tombstone.md')
MANIFEST_ENUM  = os.path.join(FIXTURES, 'manifest_min_mutant_enum.md')

# Minimal conforming md content with all required blocks (used in many tests)
_CONFORMING_HEADER = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line
"""

_CONFORMING_BLOCKS = """\
<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""

CONFORMING_MD = _CONFORMING_HEADER + '\n' + _CONFORMING_BLOCKS


def _write_tmp(content, suffix='.md'):
    f = tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name


# ============================================================
# Test harness
# ============================================================

passed = 0
failed = 0
results = []


def test(name, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        results.append(f'  PASS  {name}')
    else:
        failed += 1
        results.append(f'  FAIL  {name}' + (f': {detail}' if detail else ''))


def header(title):
    results.append(f'\n=== {title} ===')


# ============================================================
# T1: (c0) Legacy-shaped ledger -- expect C0 grandfather path
# ============================================================

header('T1: (c0) Legacy-shaped ledger fixture -- C0 grandfather path')

r_c0 = lint_mod.lint_md(PORTFOLIO_C0, label='c0-legacy')
print(r_c0.report(label='c0-legacy'))
print()

test('T1.1 c0-legacy verdict is PASS (C0 structural pass)',
     r_c0.verdict() == 'PASS',
     f'got {r_c0.verdict()}; errors={r_c0.errors}')

test('T1.2 c0-legacy path is c0-grandfather',
     r_c0.path_mode == 'c0-grandfather',
     f'got {r_c0.path_mode}')

test('T1.3 c0-legacy core_version is 0 (absent=C0)',
     r_c0.core_version == 0,
     f'got {r_c0.core_version}')

test('T1.4 c0-legacy has WARN about C0 mode',
     any('c0 grandfather' in w.lower() or 'structural' in w.lower() for w in r_c0.warnings),
     f'warnings={r_c0.warnings}')

test('T1.5 c0-legacy substrate parse OK (marker pairs found)',
     any('substrate parse ok' in f.lower() for f in r_c0.findings),
     f'findings={r_c0.findings}')

# T1.6: C0 path is structural-only -- the EXCLUDE content check (E9 item-6)
# must NOT have run: no item-6 finding/error may appear even though the
# fixture's EXCLUDE block is populated.
test('T1.6 c0-legacy C0 path did NOT run EXCLUDE content checks (structural only)',
     not any('item-6' in f for f in r_c0.findings) and
     not any('item-6' in e for e in r_c0.errors) and
     not any('EXCLUSION:' in f for f in r_c0.findings),
     f'findings={r_c0.findings}; errors={r_c0.errors}')


# ============================================================
# T2: (full) Synthetic conforming curated-full index -- expect full path
# ============================================================

header('T2: (full) Synthetic conforming curated-full index -- full path')

r_full = lint_mod.lint_md(PORTFOLIO_FULL, label='full')
print(r_full.report(label='full'))
print()

test('T2.1 full path is full',
     r_full.path_mode == 'full',
     f'got {r_full.path_mode}; errors={r_full.errors}')

test('T2.2 full core_version is 1',
     r_full.core_version == 1,
     f'got {r_full.core_version}')

test('T2.3 full ran full path checks (not C0 short-circuit)',
     r_full.path_mode == 'full',
     f'path={r_full.path_mode}')

test('T2.4 full D-20 core columns present (id/location/last_activity)',
     any('D-20' in f and 'OK' in f for f in r_full.findings),
     f'findings={r_full.findings}; errors={r_full.errors}')

test('T2.5 full verdict is PASS (direct assertion)',
     r_full.verdict() == 'PASS',
     f'verdict={r_full.verdict()}; errors={r_full.errors}')

# Report what the full path found on the curated-full content
print(f'  [T2 full-path findings on curated-full fixture]:')
for e in r_full.errors:
    print(f'    FULL-PATH-FINDING ERROR: {e}')
for w in r_full.warnings:
    print(f'    FULL-PATH-FINDING WARN: {w}')
for f in r_full.findings:
    print(f'    FULL-PATH-FINDING INFO: {f}')
print()


# ============================================================
# T3: (b) manifest-min base -- expect PASS
# ============================================================

header('T3: (b) manifest-min base -- expect PASS')

r_manifest = lint_mod.lint_md(MANIFEST_BASE, label='manifest-base')
print(r_manifest.report(label='manifest-base'))
print()

test('T3.1 manifest-base verdict is PASS',
     r_manifest.verdict() == 'PASS',
     f'errors={r_manifest.errors}; warnings={r_manifest.warnings}')

test('T3.2 manifest-base path is full',
     r_manifest.path_mode == 'full',
     f'got {r_manifest.path_mode}')

# T3.3: assert the actual parsed row/edge counts --
# 6 rows (5 active + 1 retired), 0 edges (manifest-min: edge layer not adopted).
test('T3.3 manifest-base parsed 6 rows, 0 edges (5 active + 1 retired; edges off)',
     any('Substrate parse OK: 6 rows, 0 edges' in f for f in r_manifest.findings),
     f'findings={r_manifest.findings}')

# T3.4: TOMBSTONE block parsed for retired-clone
test('T3.4 manifest-base TOMBSTONE block parsed OK',
     any('TOMBSTONE block parsed OK' in f for f in r_manifest.findings),
     f'findings={r_manifest.findings}')

# T3.5: D-20 core columns present
test('T3.5 manifest-base D-20 core columns OK',
     any('D-20' in f and 'OK' in f for f in r_manifest.findings),
     f'findings={r_manifest.findings}')


# ============================================================
# T4: Tombstone-violating mutant -- expect FAIL
# ============================================================

header('T4: Tombstone-violating mutant -- expect FAIL')

r_tomb = lint_mod.lint_md(MANIFEST_TOMB, label='manifest-tombstone-mutant')
print(r_tomb.report(label='manifest-tombstone-mutant'))
print()

test('T4.1 tombstone mutant verdict is FAIL',
     r_tomb.verdict() == 'FAIL',
     f'got {r_tomb.verdict()}; errors={r_tomb.errors}')

test('T4.2 tombstone mutant fails with E5/D-16 token (id non-empty mandate)',
     any('E5' in e and ('D-16' in e or 'id' in e.lower()) for e in r_tomb.errors),
     f'errors={r_tomb.errors}')


# ============================================================
# T5: Enum-violating mutant -- expect FAIL
# ============================================================

header('T5: Enum-violating mutant -- expect FAIL')

r_enum = lint_mod.lint_md(MANIFEST_ENUM, label='manifest-enum-mutant')
print(r_enum.report(label='manifest-enum-mutant'))
print()

test('T5.1 enum mutant verdict is FAIL',
     r_enum.verdict() == 'FAIL',
     f'got {r_enum.verdict()}; errors={r_enum.errors}')

test('T5.2 enum mutant fails for E9 item-5 / E8 class token reason',
     any(('item-5' in e or 'E9' in e or 'class' in e.lower()) and 'manual' in e
         for e in r_enum.errors),
     f'errors={r_enum.errors}')


# ============================================================
# T6: Malformed marker vs absent marker distinction (E0 / M2a)
# ============================================================

header('T6: Malformed marker vs absent (E0 M2a)')

# T6.1 Absent core marker -> C0 grandfather (PASS, no die)
# Must include EXCLUDE block (required at parse level)
_ABSENT_CONTENT = """\
<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | state | last_activity |
|:--|:--|:--|:--|
| test | /tmp/test | - | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_absent_path = _write_tmp(_ABSENT_CONTENT)
r_absent = lint_mod.lint_md(_absent_path, label='absent-core')
os.unlink(_absent_path)

test('T6.1 absent core marker -> C0 grandfather path (not die)',
     r_absent.path_mode == 'c0-grandfather' and r_absent.verdict() == 'PASS',
     f'path={r_absent.path_mode} verdict={r_absent.verdict()} errors={r_absent.errors}')

# T6.2 Malformed core marker (non-integer) -> die
_MALFORMED_CONTENT = """\
# core: notaninteger
# update-authority: tier-1 owner; tier-2 agent

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location |
|:--|:--|
| test | /tmp |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_malformed_path = _write_tmp(_MALFORMED_CONTENT)
r_malformed = lint_mod.lint_md(_malformed_path, label='malformed-core')
os.unlink(_malformed_path)

test('T6.2 malformed core marker -> die (FAIL, not C0 fallback)',
     r_malformed.verdict() == 'FAIL' and r_malformed.died,
     f'verdict={r_malformed.verdict()} died={r_malformed.died} errors={r_malformed.errors}')

test('T6.3 malformed error mentions parse-or-die or malformed',
     any('malformed' in e.lower() or 'not parseable' in e.lower() for e in r_malformed.errors),
     f'errors={r_malformed.errors}')


# ============================================================
# T7: CORE-VERSION-AHEAD -> die (fail-closed)
# ============================================================

header('T7: CORE-VERSION-AHEAD -- fail-closed')

_AHEAD_CONTENT = """\
# core: 99

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location |
|:--|:--|
| test | /tmp |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_ahead_path = _write_tmp(_AHEAD_CONTENT)
r_ahead = lint_mod.lint_md(_ahead_path, label='version-ahead')
os.unlink(_ahead_path)

test('T7.1 version-ahead path is version-ahead',
     r_ahead.path_mode == 'version-ahead',
     f'got {r_ahead.path_mode}')

test('T7.2 version-ahead verdict is FAIL (fail-closed)',
     r_ahead.verdict() == 'FAIL',
     f'got {r_ahead.verdict()}')

test('T7.3 version-ahead error mentions CORE-VERSION-AHEAD',
     any('CORE-VERSION-AHEAD' in e for e in r_ahead.errors),
     f'errors={r_ahead.errors}')


# ============================================================
# T8: JSON path die condition (D-17 full contract)
# ============================================================

header('T8: JSON parse-or-die conditions (E9 MINOR-5 / D-17)')

# T8.1 JSON syntax failure -> die
_bad_json_path = _write_tmp('{bad json', suffix='.json')
r_badjson = lint_mod.lint_json(_bad_json_path, label='bad-json')
os.unlink(_bad_json_path)

test('T8.1 JSON syntax failure -> die',
     r_badjson.verdict() == 'FAIL' and r_badjson.died,
     f'verdict={r_badjson.verdict()} errors={r_badjson.errors}')

# T8.2 JSON missing core_version as first field -> die
_misorder_path = _write_tmp(json.dumps({"id": "test", "core_version": 1}), suffix='.json')
r_misorder = lint_mod.lint_json(_misorder_path, label='json-misorder')
os.unlink(_misorder_path)

test('T8.2 JSON core_version not first field -> die',
     r_misorder.verdict() == 'FAIL' and r_misorder.died,
     f'verdict={r_misorder.verdict()} errors={r_misorder.errors}')

# T8.3 JSON valid full conforming object -> PASS (full path) per D-17
_good_json_obj = collections.OrderedDict([
    ("core_version", 1),
    ("generated", "2026-07-16"),
    ("update_authority", {"tier_1": "owner gate", "tier_2": "agent autonomous"}),
    ("discovery_markers", [".instance.conf"]),
    ("enum_sources", {"sources": ["source-a", "source-b"], "accepted_gap": False}),
    ("class_map", {"id": "declared", "location": "derived", "last_activity": "derived"}),
    ("exclusion_list", []),
    ("liveness_terminal", "weekly-report-line"),
    ("rows", [{"id": "test-row", "location": "/tmp", "last_activity": "-"}]),
])
_good_json_path = _write_tmp(json.dumps(_good_json_obj), suffix='.json')
r_goodjson = lint_mod.lint_json(_good_json_path, label='json-ok')
os.unlink(_good_json_path)

test('T8.3 JSON full conforming object -> PASS (full path) per D-17',
     r_goodjson.verdict() == 'PASS' and r_goodjson.path_mode == 'full',
     f'verdict={r_goodjson.verdict()} path={r_goodjson.path_mode} errors={r_goodjson.errors}')

# T8.4 JSON CORE-VERSION-AHEAD -> die
_ahead_json_obj = collections.OrderedDict([("core_version", 999)])
_ahead_json_path = _write_tmp(json.dumps(_ahead_json_obj), suffix='.json')
r_aheadjson = lint_mod.lint_json(_ahead_json_path, label='json-ahead')
os.unlink(_ahead_json_path)

test('T8.4 JSON CORE-VERSION-AHEAD -> die with CORE-VERSION-AHEAD token',
     r_aheadjson.verdict() == 'FAIL' and
     any('CORE-VERSION-AHEAD' in e for e in r_aheadjson.errors),
     f'verdict={r_aheadjson.verdict()} errors={r_aheadjson.errors}')

# T8.5 JSON unknown top-level key -> die (D-17)
_unknown_key_obj = collections.OrderedDict([
    ("core_version", 1),
    ("generated", "2026-07-16"),
    ("update_authority", {"tier_1": "owner", "tier_2": "agent"}),
    ("discovery_markers", [".instance.conf"]),
    ("enum_sources", {"sources": ["a", "b"], "accepted_gap": False}),
    ("class_map", {"id": "declared"}),
    ("exclusion_list", []),
    ("liveness_terminal", "console-badge"),
    ("rows", []),
    ("unknown_field", "bogus"),  # D-17: unknown top-level key = die
])
_unknown_key_path = _write_tmp(json.dumps(_unknown_key_obj), suffix='.json')
r_unknown_key = lint_mod.lint_json(_unknown_key_path, label='json-unknown-key')
os.unlink(_unknown_key_path)

test('T8.5 JSON unknown top-level key -> die (D-17)',
     r_unknown_key.verdict() == 'FAIL' and
     any('unknown' in e.lower() and 'D-17' in e for e in r_unknown_key.errors),
     f'verdict={r_unknown_key.verdict()} errors={r_unknown_key.errors}')


# ============================================================
# T9: Regression probes (grill-mandated)
# ============================================================

header('T9: Regression probes -- grill probe cases')

# T9.1 No-EXCLUDE block -> dies (D-0, E9 item-6 absent=die)
_NO_EXCLUDE = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_no_exclude_path = _write_tmp(_NO_EXCLUDE)
r_no_exclude = lint_mod.lint_md(_no_exclude_path, label='no-exclude')
os.unlink(_no_exclude_path)

test('T9.1 missing EXCLUDE block -> FAIL (FATAL-1 grill probe)',
     r_no_exclude.verdict() == 'FAIL',
     f'verdict={r_no_exclude.verdict()} errors={r_no_exclude.errors}')

test('T9.1b missing EXCLUDE error mentions EXCLUDE or substrate',
     any('EXCLUDE' in e or 'substrate' in e.lower() or 'parse' in e.lower() for e in r_no_exclude.errors),
     f'errors={r_no_exclude.errors}')

# T9.2 Bogus state value with config enum -> dies (D-18)
_BOGUS_STATE_FULL = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived, state:declared
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity | state |
|:--|:--|:--|:--|
| test-row | /tmp/test | - | totally-bogus-state!! |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_bogus_state_path = _write_tmp(_BOGUS_STATE_FULL)
lint_cfg = {'state_vocab': ['active', 'draft', 'retired', 'frozen']}
r_bogus_state = lint_mod.lint_md(_bogus_state_path, label='bogus-state', lint_config=lint_cfg)
os.unlink(_bogus_state_path)

test('T9.2 bogus state with config vocab -> FAIL (D-18 closed enum)',
     r_bogus_state.verdict() == 'FAIL',
     f'verdict={r_bogus_state.verdict()} errors={r_bogus_state.errors}')

test('T9.2b bogus state error mentions D-18 or state vocabulary',
     any('D-18' in e or 'state' in e.lower() for e in r_bogus_state.errors),
     f'errors={r_bogus_state.errors}')

# T9.3 Bogus state WITHOUT config -> WARN, not die (D-18: absent config = WARN)
_bogus_state_path2 = _write_tmp(_BOGUS_STATE_FULL)
r_bogus_state_noconfig = lint_mod.lint_md(_bogus_state_path2, label='bogus-state-noconfig')
os.unlink(_bogus_state_path2)

test('T9.3 bogus state without config -> PASS with WARN (D-18 undeclared vocabulary)',
     r_bogus_state_noconfig.verdict() == 'PASS' and
     any('D-18' in w or 'undeclared' in w.lower() for w in r_bogus_state_noconfig.warnings),
     f'verdict={r_bogus_state_noconfig.verdict()} warnings={r_bogus_state_noconfig.warnings}')

# T9.4 All-dash data row is parsed as DATA, not silently dropped (D-19)
_ALL_DASH_ROW = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| real-row | /tmp/real | 2026-07-01 |
| - | - | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
# The all-dash row (| - | - | - |) is a data row (3 cells); parser must accept
# it as DATA (D-19 positional separator), then D-20 dies on id='-'.
_all_dash_path = _write_tmp(_ALL_DASH_ROW)
r_all_dash = lint_mod.lint_md(_all_dash_path, label='all-dash-row')
os.unlink(_all_dash_path)

test('T9.4 all-dash data row parsed as data (not silently dropped per D-19)',
     r_all_dash.verdict() == 'FAIL' and
     any('D-20' in e or 'empty id' in e.lower() or 'id' in e.lower() for e in r_all_dash.errors),
     f'verdict={r_all_dash.verdict()} errors={r_all_dash.errors} findings={r_all_dash.findings}')

test('T9.4b all-dash row NOT reported as silently dropped (D-19 no-silent-drop)',
     # We should see 2 rows parsed (real-row + all-dash row), not 1 row
     any('2 rows' in f for f in r_all_dash.findings),
     f'findings={r_all_dash.findings}')

# T9.5 EXCLUDE block duplicate markers -> die (D-0)
_DUP_EXCLUDE = """\
<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test | /tmp | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_dup_exclude_path = _write_tmp(_DUP_EXCLUDE)
r_dup = lint_mod.lint_md(_dup_exclude_path, label='dup-exclude')
os.unlink(_dup_exclude_path)

test('T9.5 duplicate EXCLUDE block markers -> die (D-0 FATAL-1)',
     r_dup.verdict() == 'FAIL' and
     any('EXCLUDE' in e and ('once' in e.lower() or 'duplicate' in e.lower() or 'found 2' in e) for e in r_dup.errors),
     f'verdict={r_dup.verdict()} errors={r_dup.errors}')

# T9.6 D-6: update-authority ONLY as prose (no key line) -> die
_NO_UA_KEYLINE = """\
# core: 1
# This doc has tier-1 and tier-2 mentioned in prose but no update-authority: key line.
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_no_ua_path = _write_tmp(_NO_UA_KEYLINE)
r_no_ua = lint_mod.lint_md(_no_ua_path, label='no-ua-keyline')
os.unlink(_no_ua_path)

test('T9.6 prose tier mention without update-authority key line -> die (D-6)',
     r_no_ua.verdict() == 'FAIL' and
     any('update-authority' in e.lower() for e in r_no_ua.errors),
     f'verdict={r_no_ua.verdict()} errors={r_no_ua.errors}')

# T9.7 D-9: class-map with '=' form token -> die (= form dropped)
_EQ_CLASSMAP = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id=declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_eq_classmap_path = _write_tmp(_EQ_CLASSMAP)
r_eq_classmap = lint_mod.lint_md(_eq_classmap_path, label='eq-classmap')
os.unlink(_eq_classmap_path)

test('T9.7 class-map with "=" form -> die (D-9: = form dropped)',
     r_eq_classmap.verdict() == 'FAIL' and
     any('class-map' in e.lower() or 'col:class' in e.lower() or 'D-9' in e for e in r_eq_classmap.errors),
     f'verdict={r_eq_classmap.verdict()} errors={r_eq_classmap.errors}')

# T9.8 D-4: liveness-terminal with multiple identifiers -> die
_MULTI_LIVENESS = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: console-badge, weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_multi_liveness_path = _write_tmp(_MULTI_LIVENESS)
r_multi_liveness = lint_mod.lint_md(_multi_liveness_path, label='multi-liveness')
os.unlink(_multi_liveness_path)

test('T9.8 liveness-terminal with comma list -> die (D-4: exactly one identifier)',
     r_multi_liveness.verdict() == 'FAIL' and
     any('liveness-terminal' in e.lower() or 'D-4' in e for e in r_multi_liveness.errors),
     f'verdict={r_multi_liveness.verdict()} errors={r_multi_liveness.errors}')

# T9.9 D-3: non-dash line inside EXCLUDE block -> die
_NONDASH_EXCLUDE = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
this is a prose note, not a dash entry
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_nondash_path = _write_tmp(_NONDASH_EXCLUDE)
r_nondash = lint_mod.lint_md(_nondash_path, label='nondash-exclude')
os.unlink(_nondash_path)

test('T9.9 non-dash line in EXCLUDE block -> die (D-3)',
     r_nondash.verdict() == 'FAIL' and
     any('non-dash' in e.lower() or 'D-3' in e or 'EXCLUDE' in e for e in r_nondash.errors),
     f'verdict={r_nondash.verdict()} errors={r_nondash.errors}')

# T9.10 D-20: missing required core column -> die
_NO_LOCATION = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | last_activity |
|:--|:--|
| test-row | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_no_loc_path = _write_tmp(_NO_LOCATION)
r_no_loc = lint_mod.lint_md(_no_loc_path, label='no-location')
os.unlink(_no_loc_path)

test('T9.10 missing location column -> die (D-20)',
     r_no_loc.verdict() == 'FAIL' and
     any('D-20' in e or 'location' in e.lower() for e in r_no_loc.errors),
     f'verdict={r_no_loc.verdict()} errors={r_no_loc.errors}')

# T9.11 Conforming md -> PASS (smoke test for the whole stack)
_conf_path = _write_tmp(CONFORMING_MD)
r_conf = lint_mod.lint_md(_conf_path, label='conforming-smoke')
os.unlink(_conf_path)

test('T9.11 fully conforming md fixture -> PASS',
     r_conf.verdict() == 'PASS',
     f'verdict={r_conf.verdict()} errors={r_conf.errors} warnings={r_conf.warnings}')

# T9.12 Cross-check the shipped generic parse entrypoint (S3 shim,
# routines/lib/portfolio-core-parse.sh): it must speak the
# PORTFOLIO-PARSE-FAIL contract and die on a missing separator (D-19/F1).
PORTFOLIO_PARSE_SH = os.path.join(LIB_DIR, 'portfolio-core-parse.sh')
if os.path.exists(PORTFOLIO_PARSE_SH):
    import subprocess
    _MISSING_SEP_MD = """\
<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | kind | status |
| row1 | data | live |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
    _missing_sep_path = _write_tmp(_MISSING_SEP_MD)
    _proc = subprocess.run(
        ['bash', PORTFOLIO_PARSE_SH, _missing_sep_path],
        capture_output=True, text=True)
    os.unlink(_missing_sep_path)
    test('T9.12 portfolio-core-parse.sh dies on missing separator (D-19/F1 conformance)',
         _proc.returncode != 0 and 'PORTFOLIO-PARSE-FAIL' in _proc.stdout
         and 'D-19' in _proc.stdout and 'separator' in _proc.stdout,
         f'rc={_proc.returncode} stdout={_proc.stdout!r}')
else:
    test('T9.12 portfolio-core-parse.sh not found at expected path',
         False, f'parse entrypoint missing at {PORTFOLIO_PARSE_SH}')


# ============================================================
# T10: F1 -- separator-line validation (grill r3 FATAL)
# ============================================================

header('T10: F1 separator validation -- missing/malformed separator = die')

# T10.1 ROWS table with NO separator row: the line positionally consumed as
# separator is a data row -> must die, not silently eat it.
_MISSING_SEP_FULL = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
| eaten-row | /tmp/eaten | - |
| second-row | /tmp/second | - |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_missing_sep_md_path = _write_tmp(_MISSING_SEP_FULL)
r_missing_sep = lint_mod.lint_md(_missing_sep_md_path, label='missing-separator')
os.unlink(_missing_sep_md_path)

test('T10.1 ROWS missing separator row -> die (F1: no silent first-row eat)',
     r_missing_sep.verdict() == 'FAIL' and
     any('separator' in e.lower() and 'D-19' in e for e in r_missing_sep.errors),
     f'verdict={r_missing_sep.verdict()} errors={r_missing_sep.errors}')

# T10.2 Malformed separator (mixed cells) -> die
_MALFORMED_SEP = _MISSING_SEP_FULL.replace(
    '| eaten-row | /tmp/eaten | - |',
    '|:--| not-a-dash |:--|')
_malformed_sep_path = _write_tmp(_MALFORMED_SEP)
r_malformed_sep = lint_mod.lint_md(_malformed_sep_path, label='malformed-separator')
os.unlink(_malformed_sep_path)

test('T10.2 malformed separator (mixed cells) -> die (F1)',
     r_malformed_sep.verdict() == 'FAIL' and
     any('separator' in e.lower() for e in r_malformed_sep.errors),
     f'verdict={r_malformed_sep.verdict()} errors={r_malformed_sep.errors}')


# ============================================================
# T11: M1 -- EDGES optional-when-absent (D-0/E6)
# ============================================================

header('T11: M1 EDGES block optional (exactly-once WHEN adopted)')

# T11.1 Index without any EDGES block (edge layer not adopted) -> PASS
_EDGES_OFF = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity |
|:--|:--|:--|
| test-row | /tmp/test | - |
<!-- PORTFOLIO:ROWS:END -->
"""
_edges_off_path = _write_tmp(_EDGES_OFF)
r_edges_off = lint_mod.lint_md(_edges_off_path, label='edges-off')
os.unlink(_edges_off_path)

test('T11.1 edges-off index (no EDGES block) -> PASS (M1: optional when not adopted)',
     r_edges_off.verdict() == 'PASS' and
     any('0 edges' in f for f in r_edges_off.findings),
     f'verdict={r_edges_off.verdict()} errors={r_edges_off.errors} findings={r_edges_off.findings}')

# T11.2 Duplicate EDGES blocks -> die (D-0: at most once)
_DUP_EDGES = _EDGES_OFF + """
<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type |
|:--|:--|
<!-- PORTFOLIO:EDGES:END -->
"""
_dup_edges_path = _write_tmp(_DUP_EDGES)
r_dup_edges = lint_mod.lint_md(_dup_edges_path, label='dup-edges')
os.unlink(_dup_edges_path)

test('T11.2 duplicate EDGES blocks -> die (D-0 registry discipline)',
     r_dup_edges.verdict() == 'FAIL' and
     any('EDGES' in e and ('once' in e.lower() or 'found 2' in e) for e in r_dup_edges.errors),
     f'verdict={r_dup_edges.verdict()} errors={r_dup_edges.errors}')

# T11.3 manifest-min fixture (edges off, spec-conforming shape) -> PASS
test('T11.3 manifest-min fixture with edges off -> PASS (M1)',
     r_manifest.verdict() == 'PASS',
     f'verdict={r_manifest.verdict()} errors={r_manifest.errors}')


# ============================================================
# T12: M2/M3 -- JSON semantic parity (E9) + JSON grandfather (E0)
# ============================================================

header('T12: JSON semantic parity (M2) + JSON C0 grandfather (M3)')


def _json_base(**overrides):
    """Conforming JSON index object; overrides replace/add/delete (None) keys."""
    obj = collections.OrderedDict([
        ("core_version", 1),
        ("generated", "2026-07-16"),
        ("update_authority", {"tier_1": "owner gate", "tier_2": "agent autonomous"}),
        ("discovery_markers", [".instance.conf"]),
        ("enum_sources", {"sources": ["source-a", "source-b"], "accepted_gap": False}),
        ("class_map", {"id": "declared", "location": "derived",
                       "last_activity": "derived", "state": "declared"}),
        ("exclusion_list", []),
        ("liveness_terminal", "console-badge"),
        ("rows", [{"id": "r1", "location": "/tmp/r1", "last_activity": "-", "state": "-"}]),
    ])
    for k, v in overrides.items():
        if v is None:
            obj.pop(k, None)
        else:
            obj[k] = v
    return obj


def _lint_json_obj(obj, label, lint_config=None):
    p = _write_tmp(json.dumps(obj), suffix='.json')
    r = lint_mod.lint_json(p, label=label, lint_config=lint_config)
    os.unlink(p)
    return r


# T12.1 (M3) core_version marker ABSENT -> C0 grandfather, structural + WARN
r_json_c0 = _lint_json_obj(collections.OrderedDict([
    ("rows", [{"id": "r1", "location": "/tmp", "last_activity": "-"}]),
]), 'json-c0')
test('T12.1 JSON core_version absent -> C0 grandfather PASS + WARN (M3/E0)',
     r_json_c0.verdict() == 'PASS' and r_json_c0.path_mode == 'c0-grandfather' and
     any('structural' in w.lower() for w in r_json_c0.warnings),
     f'verdict={r_json_c0.verdict()} path={r_json_c0.path_mode} errors={r_json_c0.errors}')

# T12.2 (M3) C0 structural checks still bite: retired row with empty id -> die
r_json_c0_bad = _lint_json_obj(collections.OrderedDict([
    ("rows", [{"id": "-", "state": "retired"}]),
]), 'json-c0-badid')
test('T12.2 JSON C0 retired row with empty id -> die (E5/D-16 structural at C0)',
     r_json_c0_bad.verdict() == 'FAIL' and
     any('E5/D-16' in e for e in r_json_c0_bad.errors),
     f'verdict={r_json_c0_bad.verdict()} errors={r_json_c0_bad.errors}')

# T12.3 (M3) marker PRESENT but malformed still dies (0 and non-integer)
r_json_zero = _lint_json_obj(_json_base(core_version=0), 'json-core-zero')
r_json_str = _lint_json_obj(_json_base(core_version="notanint"), 'json-core-str')
test('T12.3 JSON core_version 0 / non-integer -> die (malformed marker, not C0 fallback)',
     r_json_zero.verdict() == 'FAIL' and r_json_str.verdict() == 'FAIL',
     f'zero={r_json_zero.errors} str={r_json_str.errors}')

# T12.4 (M2/D-12) retired row without tombstones key -> die
r_json_ret_no_tomb = _lint_json_obj(_json_base(
    rows=[{"id": "dead-1", "location": "/tmp", "last_activity": "-", "state": "retired"}]),
    'json-retired-no-tomb')
test('T12.4 JSON retired row without tombstones -> die (D-12 cross-check parity)',
     r_json_ret_no_tomb.verdict() == 'FAIL' and
     any('tombstones' in e for e in r_json_ret_no_tomb.errors),
     f'verdict={r_json_ret_no_tomb.verdict()} errors={r_json_ret_no_tomb.errors}')

# T12.5 (M2/D-12) retired row WITH matching tombstone entry -> PASS
r_json_ret_ok = _lint_json_obj(_json_base(
    rows=[{"id": "dead-1", "location": "/tmp", "last_activity": "-", "state": "retired"}],
    tombstones=[{"id": "dead-1", "date": "2026-07-16", "reason": "test retirement"}]),
    'json-retired-ok')
test('T12.5 JSON retired row with matching tombstone -> PASS',
     r_json_ret_ok.verdict() == 'PASS',
     f'verdict={r_json_ret_ok.verdict()} errors={r_json_ret_ok.errors}')

# T12.6 (M2/D-12) tombstone id mismatch -> die
r_json_tomb_mismatch = _lint_json_obj(_json_base(
    rows=[{"id": "dead-1", "location": "/tmp", "last_activity": "-", "state": "retired"}],
    tombstones=[{"id": "other-id", "date": "2026-07-16"}]),
    'json-tomb-mismatch')
test('T12.6 JSON tombstone id mismatch -> die (D-12)',
     r_json_tomb_mismatch.verdict() == 'FAIL' and
     any('no matching' in e for e in r_json_tomb_mismatch.errors),
     f'errors={r_json_tomb_mismatch.errors}')

# T12.7 (M2/D-12) duplicate tombstone ids -> die
r_json_tomb_dup = _lint_json_obj(_json_base(
    tombstones=[{"id": "x", "date": "2026-07-16"}, {"id": "x", "date": "2026-07-15"}]),
    'json-tomb-dup')
test('T12.7 JSON duplicate tombstone ids -> die (D-12)',
     r_json_tomb_dup.verdict() == 'FAIL' and
     any('duplicate' in e.lower() for e in r_json_tomb_dup.errors),
     f'errors={r_json_tomb_dup.errors}')

# T12.8 (M2/D-18) bogus state, no config -> PASS with WARN (undeclared vocabulary)
r_json_bogus_state = _lint_json_obj(_json_base(
    rows=[{"id": "r1", "location": "/tmp", "last_activity": "-", "state": "totally-bogus"}]),
    'json-bogus-state')
test('T12.8 JSON bogus state without config -> PASS + WARN (D-18 parity)',
     r_json_bogus_state.verdict() == 'PASS' and
     any('undeclared' in w.lower() for w in r_json_bogus_state.warnings),
     f'verdict={r_json_bogus_state.verdict()} warnings={r_json_bogus_state.warnings}')

# T12.9 (M2/D-18) bogus state WITH config vocab -> die
r_json_bogus_state_cfg = _lint_json_obj(_json_base(
    rows=[{"id": "r1", "location": "/tmp", "last_activity": "-", "state": "totally-bogus"}]),
    'json-bogus-state-cfg', lint_config={'state_vocab': ['active', 'draft']})
test('T12.9 JSON bogus state with config vocab -> die (D-18 closed enum parity)',
     r_json_bogus_state_cfg.verdict() == 'FAIL' and
     any('D-18' in e for e in r_json_bogus_state_cfg.errors),
     f'errors={r_json_bogus_state_cfg.errors}')

# T12.10 (M2/D-14) generated malformed -> die; generated absent -> PASS + WARN
r_json_gen_bad = _lint_json_obj(_json_base(generated="yesterday-ish"), 'json-gen-bad')
r_json_gen_abs = _lint_json_obj(_json_base(generated=None), 'json-gen-absent')
test('T12.10 JSON generated malformed=die / absent=WARN (D-14 parity)',
     r_json_gen_bad.verdict() == 'FAIL' and
     any('D-14' in e for e in r_json_gen_bad.errors) and
     r_json_gen_abs.verdict() == 'PASS' and
     any('generated stamp absent' in w for w in r_json_gen_abs.warnings),
     f'bad={r_json_gen_bad.errors} abs={r_json_gen_abs.warnings}')

# T12.11 (M2/D-3) exclusion entry grammar: non-ISO date / extra field -> die
r_json_excl_date = _lint_json_obj(_json_base(
    exclusion_list=[{"item": "x", "reason": "y", "date": "16/07/2026"}]),
    'json-excl-date')
r_json_excl_extra = _lint_json_obj(_json_base(
    exclusion_list=[{"item": "x", "reason": "y", "date": "2026-07-16", "extra": "z"}]),
    'json-excl-extra')
test('T12.11 JSON exclusion entry bad date / extra field -> die (D-3 parity)',
     r_json_excl_date.verdict() == 'FAIL' and r_json_excl_extra.verdict() == 'FAIL',
     f'date={r_json_excl_date.errors} extra={r_json_excl_extra.errors}')

# T12.12 (M2/D-20) empty row id -> die; class_map missing core column -> die
r_json_empty_id = _lint_json_obj(_json_base(
    rows=[{"id": "-", "location": "/tmp", "last_activity": "-", "state": "-"}]),
    'json-empty-id')
_cm_noloc = {"id": "declared", "last_activity": "derived", "state": "declared"}
r_json_noloc = _lint_json_obj(_json_base(
    class_map=_cm_noloc,
    rows=[{"id": "r1", "last_activity": "-", "state": "-"}]),
    'json-noloc')
test('T12.12 JSON empty id / missing location column -> die (D-20 parity)',
     r_json_empty_id.verdict() == 'FAIL' and
     any('D-20' in e for e in r_json_empty_id.errors) and
     r_json_noloc.verdict() == 'FAIL' and
     any('D-20' in e for e in r_json_noloc.errors),
     f'id={r_json_empty_id.errors} noloc={r_json_noloc.errors}')

# T12.13 (m6) JSON row key not in class_map -> die
r_json_rowkey = _lint_json_obj(_json_base(
    rows=[{"id": "r1", "location": "/tmp", "last_activity": "-",
           "state": "-", "rogue_column": "x"}]),
    'json-rogue-rowkey')
test('T12.13 JSON row key not in class_map -> die (D-17)',
     r_json_rowkey.verdict() == 'FAIL' and
     any('not in class_map' in e for e in r_json_rowkey.errors),
     f'errors={r_json_rowkey.errors}')

# T12.14 (M2/D-7) JSON accepted_gap with 2 sources -> die; 1 source no gap -> die
r_json_gap2 = _lint_json_obj(_json_base(
    enum_sources={"sources": ["a", "b"], "accepted_gap": True}), 'json-gap2')
r_json_one_nogap = _lint_json_obj(_json_base(
    enum_sources={"sources": ["a"], "accepted_gap": False}), 'json-one-nogap')
test('T12.14 JSON enum_sources D-7 both directions -> die',
     r_json_gap2.verdict() == 'FAIL' and r_json_one_nogap.verdict() == 'FAIL',
     f'gap2={r_json_gap2.errors} one={r_json_one_nogap.errors}')


# ============================================================
# T13: minors -- m1 (D-2), m2 (ISO anchors), m3 (older window), m4 (dup keys)
# ============================================================

header('T13: minors m1-m4')

_HDR_TEMPLATE = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: {marker}
# enum-sources: {sources}
# class-map: id:declared, location:derived, last_activity:derived
# liveness-terminal: weekly-report-line
{extra}
"""


def _md_with(marker='poc-dir-listing', sources='source-a, source-b', extra=''):
    return _HDR_TEMPLATE.format(marker=marker, sources=sources, extra=extra) + '\n' + _CONFORMING_BLOCKS


def _lint_md_str(content, label, lint_version=None):
    p = _write_tmp(content)
    kwargs = {}
    if lint_version is not None:
        kwargs['lint_version'] = lint_version
    r = lint_mod.lint_md(p, label=label, **kwargs)
    os.unlink(p)
    return r


# T13.1 (m1) discovery-marker ",,," -> die (no non-empty identifier)
r_m1 = _lint_md_str(_md_with(marker=',,,'), 'm1-commas')
test('T13.1 discovery-marker ",,," -> die (D-2: at least one non-empty identifier)',
     r_m1.verdict() == 'FAIL' and
     any('item-3' in e for e in r_m1.errors),
     f'errors={r_m1.errors}')

# T13.2 (m2) generated stamp with trailing garbage -> die (anchored regex);
# month out of range -> die
r_m2a = _lint_md_str(_md_with(extra='# generated: 2026-07-16garbage'), 'm2-suffix')
r_m2b = _lint_md_str(_md_with(extra='# generated: 2026-13-01'), 'm2-month13')
test('T13.2 generated "2026-07-16garbage" / "2026-13-01" -> die (m2: anchored + ranges)',
     r_m2a.verdict() == 'FAIL' and any('D-14' in e for e in r_m2a.errors) and
     r_m2b.verdict() == 'FAIL' and any('D-14' in e for e in r_m2b.errors),
     f'suffix={r_m2a.errors} month={r_m2b.errors}')

# T13.3 (m3) S=2 simulation, C=1 within window: version-1 published rules ARE
# this lint's full checks -- full path runs (D-20 finding present) + WARN.
r_m3_window = _lint_md_str(CONFORMING_MD, 'm3-window', lint_version=2)
test('T13.3 S=2 C=1 older-window -> version-1 rules applied (full checks) + WARN',
     r_m3_window.verdict() == 'PASS' and r_m3_window.path_mode == 'older-window' and
     any('older core 1' in w for w in r_m3_window.warnings) and
     any('D-20' in f and 'OK' in f for f in r_m3_window.findings),
     f'path={r_m3_window.path_mode} warnings={r_m3_window.warnings} errors={r_m3_window.errors}')

# T13.4 (m3) S=3 simulation, C=1 older than window: structural checks only +
# WARN -- must NOT fall through to full checks / E8 die.
_OLDER_STRUCTURAL_MD = '# core: 1\n\n' + _CONFORMING_BLOCKS
r_m3_structural = _lint_md_str(_OLDER_STRUCTURAL_MD, 'm3-structural', lint_version=3)
test('T13.4 S=3 C=1 older-than-window -> structural only + WARN, no E8 die (m3)',
     r_m3_structural.verdict() == 'PASS' and
     r_m3_structural.path_mode == 'older-structural' and
     any('structural checks only' in w for w in r_m3_structural.warnings) and
     not any('E8' in e for e in r_m3_structural.errors),
     f'path={r_m3_structural.path_mode} verdict={r_m3_structural.verdict()} errors={r_m3_structural.errors}')

# T13.5 (m4) duplicate contradictory header keys -> die (post-lock clarification)
_DUP_CORE = '# core: 1\n# core: 2\n' + _CONFORMING_BLOCKS
r_m4a = _lint_md_str(_DUP_CORE, 'm4-dup-core')
_DUP_CLASSMAP = _md_with(extra='# class-map: id:judgment')
r_m4b = _lint_md_str(_DUP_CLASSMAP, 'm4-dup-classmap')
test('T13.5 duplicate core / class-map header keys -> die (m4: first-wins forbidden)',
     r_m4a.verdict() == 'FAIL' and
     any('duplicate header key' in e for e in r_m4a.errors) and
     r_m4b.verdict() == 'FAIL' and
     any('duplicate header key' in e for e in r_m4b.errors),
     f'core={r_m4a.errors} classmap={r_m4b.errors}')


# ============================================================
# T14: m6 -- missing die-path probes (md)
# ============================================================

header('T14: m6 die-path probes (md)')

_STATE_HDR = """\
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = agent autonomous
# discovery-marker: poc-dir-listing
# enum-sources: source-a, source-b
# class-map: id:declared, location:derived, last_activity:derived, state:declared
# liveness-terminal: weekly-report-line
"""


def _state_md(rows, tombstone_block='', exclude_lines=''):
    return (_STATE_HDR + f"""
<!-- PORTFOLIO:EXCLUDE:BEGIN -->
{exclude_lines}<!-- PORTFOLIO:EXCLUDE:END -->
{tombstone_block}
<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity | state |
|:--|:--|:--|:--|
{rows}<!-- PORTFOLIO:ROWS:END -->
""")


_TOMB_OTHER = """
<!-- PORTFOLIO:TOMBSTONE:BEGIN -->
- other-id | 2026-07-16 | -
<!-- PORTFOLIO:TOMBSTONE:END -->
"""

# T14.1 tombstone id-mismatch: retired row has no matching entry -> die
r_t141 = _lint_md_str(_state_md('| dead-1 | /tmp | - | retired |\n', _TOMB_OTHER), 'tomb-mismatch')
test('T14.1 md tombstone id-mismatch -> die (D-12 cross-check)',
     r_t141.verdict() == 'FAIL' and
     any('no matching TOMBSTONE entry' in e for e in r_t141.errors),
     f'errors={r_t141.errors}')

# T14.2 duplicate tombstone ids -> die
_TOMB_DUP = """
<!-- PORTFOLIO:TOMBSTONE:BEGIN -->
- dead-1 | 2026-07-16 | -
- dead-1 | 2026-07-15 | -
<!-- PORTFOLIO:TOMBSTONE:END -->
"""
r_t142 = _lint_md_str(_state_md('| dead-1 | /tmp | - | retired |\n', _TOMB_DUP), 'tomb-dup')
test('T14.2 md duplicate tombstone ids -> die (D-12)',
     r_t142.verdict() == 'FAIL' and
     any('duplicate tombstone id' in e for e in r_t142.errors),
     f'errors={r_t142.errors}')

# T14.3 tombstone block absent with retired row -> die
r_t143 = _lint_md_str(_state_md('| dead-1 | /tmp | - | retired |\n'), 'tomb-absent')
test('T14.3 md TOMBSTONE block absent with retired row -> die (D-12 presence rule)',
     r_t143.verdict() == 'FAIL' and
     any('block absent' in e for e in r_t143.errors),
     f'errors={r_t143.errors}')

# T14.4 D-3 exclusion variants: non-ISO date; four fields -> die
r_t144a = _lint_md_str(
    _state_md('| r1 | /tmp | - | - |\n',
              exclude_lines='- item-x | some reason | 16/07/2026\n'),
    'excl-baddate')
r_t144b = _lint_md_str(
    _state_md('| r1 | /tmp | - | - |\n',
              exclude_lines='- item-x | some reason | 2026-07-16 | extra-field\n'),
    'excl-fourfields')
test('T14.4 md exclusion non-ISO date / four fields -> die (D-3 variants)',
     r_t144a.verdict() == 'FAIL' and
     any('ISO' in e for e in r_t144a.errors) and
     r_t144b.verdict() == 'FAIL' and
     any('exactly 3' in e for e in r_t144b.errors),
     f'date={r_t144a.errors} four={r_t144b.errors}')

# T14.5 D-7 both md directions: 2+ sources + accepted-gap -> die;
# one source without accepted-gap -> die
r_t145a = _lint_md_str(_md_with(sources='source-a, source-b + accepted-gap'), 'd7-gap2')
r_t145b = _lint_md_str(_md_with(sources='source-a'), 'd7-one-nogap')
test('T14.5 md enum-sources D-7 both directions -> die',
     r_t145a.verdict() == 'FAIL' and
     any('accepted-gap' in e for e in r_t145a.errors) and
     r_t145b.verdict() == 'FAIL' and
     any('fewer than two' in e for e in r_t145b.errors),
     f'gap2={r_t145a.errors} one={r_t145b.errors}')

# T14.6 mutant-enum fixture cells un-scrambled but die-point intact (m6 hygiene)
test('T14.6 mutant-enum fixture still dies at its intended die-point (class token)',
     r_enum.verdict() == 'FAIL' and
     any('manual' in e for e in r_enum.errors),
     f'errors={r_enum.errors}')


# ============================================================
# Summary
# ============================================================

print('\n' + '='*60)
print('SELF-TEST SUMMARY')
print('='*60)
for line in results:
    print(line)
print()
print(f'TOTAL: {passed + failed} tests -- {passed} PASSED, {failed} FAILED')
print()

if failed > 0:
    sys.exit(1)
else:
    sys.exit(0)
