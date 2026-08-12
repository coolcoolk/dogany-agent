# fleet-manifest-mutant-tombstone.md -- tombstone-violating mutant
# Based on manifest_min.md; SINGLE mutation: the retired row has
# state=retired (tombstone state token per E5) but id="-" (the md empty
# sentinel -- effectively missing mandatory id field).
# Expected lint result: FAIL with E5/D-16 token (id missing on retired row).
# Note: TOMBSTONE block cross-check also fails because the row id "-" cannot
# match any tombstone entry (D-12 cross-check).
# Edge layer not adopted (mirrors base fixture; D-0/E6).
#
# E9 header contract items (conforming):
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = scanner/agent autonomous
# discovery-marker: poc-dir-listing (~/fleet/poc/), launchd-label (com.example.fleet.*)
# enum-sources: poc-dir-listing, launchd-labels
# class-map: id:declared, location:derived, state:declared, last_activity:derived
# liveness-terminal: weekly-report-line
#
# Exclusion list (empty):

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:TOMBSTONE:BEGIN -->
- retired-clone | 2026-07-16 | retired poc clone
<!-- PORTFOLIO:TOMBSTONE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | state | last_activity |
|:--|:--|:--|:--|
| unit-a | ~/fleet/poc/UnitA | - | - |
| unit-b | ~/fleet/poc/UnitB | - | - |
| unit-c | ~/fleet/poc/UnitC | - | - |
| unit-d | ~/fleet/poc/UnitD | - | - |
| unit-e | ~/fleet/poc/UnitE | - | - |
| - | ~/fleet/poc/RetiredClone | retired | 2026-07-16 |
<!-- PORTFOLIO:ROWS:END -->
