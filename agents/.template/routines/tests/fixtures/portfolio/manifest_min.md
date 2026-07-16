# fleet-manifest.md -- manifest-min fixture (portfolio core test suite)
# Preset: manifest-min (persisted minimal manifest, ls/conf-derived where
#   possible, NO state machinery for active rows, narrative worklog untouched,
#   edges OFF until E6 trigger).
# Edge layer NOT adopted (C.3 manifest-min: "edges off until trigger").
#   Per D-0/E6 the EDGES block appears exactly once WHEN adopted -- so this
#   fixture carries NO EDGES block.
# Synthetic estate (de-personalized): a five-unit PoC fleet plus one retired
#   clone. Ground truth sources:
#   Source 1: ~/fleet/poc/ directories: UnitA, UnitB, UnitC, UnitD, UnitE
#   Source 2: launchd labels com.example.fleet.* (retired-clone label gone)
# retired-clone: designated day-one retired row per E5.
#   Tombstone written by the designated writer off the reconcile diff.
#   Core fields: id (retired-clone), date (2026-07-16). Reason optional.
#   State token = "retired" per E5 ("a row state token (retired/frozen)").
#   TOMBSTONE block carries the id+date mandatory fields (D-12/D-13).
#   last_activity DOES NOT double as the tombstone date (D-13: distinct facts).
#
# E9 header contract items (minimal conforming):
# core: 1
# update-authority: tier-1 = owner gate; tier-2 = scanner/agent autonomous
# discovery-marker: poc-dir-listing (~/fleet/poc/), launchd-label (com.example.fleet.*)
# enum-sources: poc-dir-listing, launchd-labels
# class-map: id:declared, location:derived, state:declared, last_activity:derived
# liveness-terminal: weekly-report-line
#
# Exclusion list (none today -- empty list is legal per E9 item-6):

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
