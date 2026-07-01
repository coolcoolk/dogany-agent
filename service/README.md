# service/ — shared module layer (SDK for skills)

Reusable query/CRUD helpers over the lifekit DB + utility modules (e.g. local-time).
Agents import these when authoring skills. NOT launchd (each agent's plists live
self-contained under agents/<name>/bridge/ and agents/<name>/routines/).
To be built with 사용자 (DGN-039 project/task system lands its CRUD here).
