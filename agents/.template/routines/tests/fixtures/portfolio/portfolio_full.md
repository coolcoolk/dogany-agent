# PORTFOLIO.md -- synthetic conforming fixture (curated-full, full lint path)
# Preset: curated-full (persisted curated+lint index, derive discoverable
#   rows, extension columns, edges ON, exclusion list in use).
# Synthetic estate (de-personalized): two maintained repos, three consuming
#   agent instances, one remote semi-managed instance, one frozen legacy repo.
# Purpose: force the FULL lint path (core: 1) over a rich curated-full shape:
#   extension columns, pointer grammar cells (see:/pin:), mechanism tags
#   ([live]/[GAP]), an EDGES block, a populated exclusion list, and a
#   role=frozen row. NOTE: `role` is instance EXTENSION vocabulary -- the core
#   tombstone cross-check keys on the `state` column only (D-12); this index
#   has no state column, so role=frozen requires NO tombstone here.
#
# E9 header contract items:
# core: 1
# generated: 2026-07-16T12:00
# update-authority: tier-1 = owner gate (schema/column changes); tier-2 = agent autonomous (row/edge content)
# discovery-marker: .instance.conf
# enum-sources: agent-home dirs (~/agents/), repo config glob
# class-map: id:declared, location:derived, last_activity:derived, kind:declared, visibility:declared, role:judgment, release_path:judgment, health:derived
# liveness-terminal: console-badge
#
# Update authority (prose documentation -- legal per D-6, does not substitute
# for the key line above):
#   Tier 1 (owner gate): schema / definition changes.
#   Tier 2 (agent autonomous): row/edge content updates.

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
- scratch-unit | test debris; orphan scheduler label pointing at a temp path | 2026-07-16
- demo-clone | one-off demo copy; never a managed unit | 2026-07-16
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | location | last_activity | kind | visibility | role | release_path | health |
|:--|:--|:--|:--|:--|:--|:--|:--|
| framework-repo | git.example.com/acme/framework | - | repo | public | maintainer | release script; gate=owner [live] | weekly-check a |
| gateway-repo | git.example.com/acme/gateway | - | repo | public | maintainer | direct push; gate=owner [live] | weekly-check b |
| agent-one | ~/agents/AgentOne | - | instance | private | version-consumer | self-update [live] | weekly-check c |
| agent-two | ~/agents/AgentTwo | - | instance | private | version-consumer | update script [live] | weekly-check c |
| agent-three | ~/agents/AgentThree | - | instance | private | version-consumer | update script [live] | see:product/notes.md |
| remote-one | - | - | instance | private | semi-managed | manual update by operator [live] | GAP |
| legacy-repo | git.example.com/acme/legacy | - | repo | public | frozen | none [n-a] | n-a |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type | direction | mechanism | discipline_ref | health |
|:--|:--|:--|:--|:--|:--|
| E1 | vendoring | gateway-repo -> framework-repo | re-vendor copy + SHA pin [live] | topology doc sec 3 | weekly-check b |
| E2a | version-consume | framework-repo -> agent-one | self-update [live] | release policy | weekly-check c |
| E2b | version-consume | framework-repo -> agent-two | update script [live] | release policy | weekly-check c |
| E2c | version-consume | framework-repo -> remote-one | manual update by operator [live]; remote notice [GAP] | release policy | GAP |
| E3 | approval-gate | owner -> framework-repo | public push requires owner approval [live] | pin:RULES.md | n-a |
<!-- PORTFOLIO:EDGES:END -->
