# service/ — shared module layer (SDK for skills)

Reusable SDK surfaces skills import instead of raw data layers (e.g. mailer).
Agents import these when authoring skills. NOT launchd (each agent's plists live
self-contained under agents/<name>/bridge/ and agents/<name>/routines/).
The lifekit facade (service/lifekit/) moved to the independent lifekit pack
(DGN-803 LS-5); pack_install.sh places it back here at kit activation.
