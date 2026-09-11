# PORTFOLIO.md -- synthetic C0 legacy-ledger fixture (grandfather path)
# Shape: a REAL-WORLD pre-core ledger -- no `core:` marker anywhere, no
#   location/last_activity core columns (a `repo` column instead), prose
#   governance header, populated EXCLUDE block, ROWS + EDGES blocks.
# Purpose: reproduce what a live legacy ledger asserts under the shipped
#   lint -- marker ABSENT = C=0 grandfather (E0): structural checks only +
#   WARN; header contract items 2-7 NOT checked; EXCLUDE content checks
#   (E9 item-6) NOT run; D-20 core-column mandate NOT applied.
#   This is the intentional soft-migration entry state: the absence is
#   meaningful, and cutover to `core: 1` is an explicit dated opt-in.
#
# Conflict rule: prose canon ALWAYS WINS over this ledger.
#
# Update authority (three tiers, prose form only -- no key line; legal at C0):
#   Tier 1 (owner gate): schema / definition changes.
#   Tier 2 (agent autonomous): row/edge content updates.
#   Tier 3 (agent autonomous routine): judgment execution.

<!-- PORTFOLIO:EXCLUDE:BEGIN -->
- scratch-unit | test debris; orphan scheduler label | 2026-07-16
- demo-clone | one-off demo copy; never a managed unit | 2026-07-16
<!-- PORTFOLIO:EXCLUDE:END -->

<!-- PORTFOLIO:ROWS:BEGIN -->
| id | kind | repo | visibility | role | health |
|:--|:--|:--|:--|:--|:--|
| framework-repo | repo | git.example.com/acme/framework | public | maintainer | weekly-check a |
| gateway-repo | repo | git.example.com/acme/gateway | public | maintainer | weekly-check b |
| agent-one | instance | ~/agents/AgentOne | private | version-consumer | weekly-check c |
| agent-two | instance | ~/agents/AgentTwo | private | version-consumer | weekly-check c |
| remote-one | instance | - | private | semi-managed | GAP |
| legacy-repo | repo | git.example.com/acme/legacy | public | frozen | n-a |
<!-- PORTFOLIO:ROWS:END -->

<!-- PORTFOLIO:EDGES:BEGIN -->
| # | type | direction | mechanism | health |
|:--|:--|:--|:--|:--|
| E1 | vendoring | gateway-repo -> framework-repo | re-vendor copy + SHA pin [live] | weekly-check b |
| E2 | version-consume | framework-repo -> agent-one | self-update [live] | weekly-check c |
| E3 | approval-gate | owner -> framework-repo | public push requires owner approval [live] | n-a |
<!-- PORTFOLIO:EDGES:END -->
