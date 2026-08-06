# fleet-manifest-mutant-enum.md -- enum-violating mutant
# Based on manifest_min.md; SINGLE mutation: class-map has unknown class
# token "manual" (enum violation: E9 item-5 / E8 requires class tokens from
# {derived, declared, judgment}). All other content mirrors the base fixture.
# Expected lint result: FAIL -- E9 item-5 malformed: unknown provenance class "manual".
#
# E9 header contract items (intentionally malformed class-map):
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = scanner/agent autonomous
# discovery-marker: poc-dir-listing (~/fleet/poc/), launchd-label (com.example.fleet.*)
# enum-sources: poc-dir-listing, launchd-labels
# class-map: id:declared, location:manual, state:declared, last_activity:derived
# liveness-terminal: weekly-report-line
#
# Exclusion list (empty):

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:TOMBSTONE:BEGIN -->
- retired-clone | 2026-07-16 | retired poc clone; directory removed from fleet
<!-- PORTFOLIO:TOMBSTONE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | state | last_activity |
|:--|:--|:--|:--|
| unit-a | ~/fleet/poc/UnitA | - | - |
| unit-b | ~/fleet/poc/UnitB | - | - |
| unit-c | ~/fleet/poc/UnitC | - | - |
| unit-d | ~/fleet/poc/UnitD | - | - |
| unit-e | ~/fleet/poc/UnitE | - | - |
| retired-clone | ~/fleet/poc/RetiredClone | retired | 2026-07-16 |
<!-- PORTFOLIO:ROWS:END -->
