# service/ — shared module layer (SDK for skills)

Reusable SDK surfaces skills import instead of raw data layers (e.g. mailer).
Agents import these when authoring skills. NOT launchd (each agent's plists live
self-contained under agents/<name>/bridge/ and agents/<name>/routines/).
The lifekit facade (service/lifekit/) moved to the independent lifekit kit
(DGN-803 LS-5); pack_install.sh places it back here at kit activation.
service/calendar/ (DGN-1854) is the calendar connection seam the lifekit mirror
calls instead of the gws CLI: a closed verb table, a Google backend over
service.google, gws-parity errors, and `python3 -m service.calendar status|resolve --json`.
