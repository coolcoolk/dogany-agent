# Changelog

All notable user-facing changes to Dogany are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] - 1.5.1

### Changed
- Remote version check is now default ON. Instances on other machines will
  automatically receive a nudge at session start when a newer framework version
  is available, with no configuration required. To opt out, set
  DOGANY_VERSION_CHECK=0 in your instance .telegram_bot/.env. The legacy opt-in
  value (DOGANY_VERSION_CHECK=1) remains valid and keeps the check on. (DGN-335)

### Added
- Version-check throttle: the remote GET runs at most once every 6 hours. The
  last successful result is cached in .telegram_bot/state/version-check-cache.
  Cache read/write failures are silently ignored (fail-open). (DGN-335)

## [1.5.0] - 2026-07-16

### Added
- Single-option [[OPTIONS]] buttons now render as a proper Telegram button
  instead of being silently suppressed. The two-part fix covers the render
  path and the TextBlock-precedence assembly; a scaffold-leak guard ensures
  agent thinking text can no longer bleed into the button payload. (DGN-325,
  DGN-285)
- Self-restart now guards against interrupting a live user session. If a
  session is active when a restart is triggered, the restart is deferred
  until the session is idle. (DGN-328)
- Project folder path sanitization now follows the Claude Code rule: all
  non-alphanumeric characters (not just slashes) become dashes in the
  transcript glob path. Fixes silent consolidation failure for usernames with
  dots or underscores. (DGN-295)
- Skills and routines now carry a user-facing display name in their
  frontmatter. Agents use the display name in menus and confirmations instead
  of the raw directory name. Existing skills have been backfilled; a short
  i18n name tier is available for localized surfaces. (DGN-324)
- Cron-guard failure notifications now lead with the friendly display name
  instead of the raw job label, so the alert is readable without knowing the
  cron internals.
- Reminder cancel now works by index: the agent lists active reminders by
  number and accepts an index to cancel, with no requirement to remember or
  type the machine label. (DGN-324 GAP-6)
- Onboarding batch: identity fields now start blank (no slug or user-label
  pre-fill), the first message pairs the agent greeting with the first
  question in a single turn, the migration-path completion branch now closes
  cleanly with a single guidance line and no empty menu, and neutral button
  labels are now enforced throughout. Role-adaptive quick-start options
  (option 2 adapts to the filled role instead of being hard-coded to
  record-keeping) are included in this batch. (DGN-277)
- Onboarding address guard and ambient-label hardening: the agent now avoids
  accidentally using the agent's own name as the user's address before the
  user's name has been confirmed. Tone question style and button spec
  tightened. (DGN-284)
- Agent-to-agent migration request wired at onboarding completion: when a
  user migrates from another agent, the onboarding flow now dispatches a
  migration.request handoff so the source agent can forward data
  automatically. (DGN-277 f9)
- cron-register skill revision round: test-fire exception documented (skip
  full re-register when only the schedule changes and the job ran cleanly
  that day), time-rename rule documented (label/script/log must all be
  renamed together and the old job trashed), ProcessType=Interactive added to
  the template plist for macOS display-sleep safety, and the worker-script
  pattern (task script as entrypoint, push.sh called internally) documented.
  Seven previously undocumented practices backfilled in the same round.
  (DGN-292)
- upstream-report skill: agents can now file a structured framework proposal
  as a GitHub issue on the canonical repos (dogany-agent or
  claude-code-telegram routing, coolcoolk identity gate, outbox-draft
  fallback). Self-maintained repos use an internal ticket + direct fix path
  instead. (DGN-293)
- Morning brief: title-prefix exclusion is now config-driven
  (BRIEF_TITLE_EXCLUDE_PREFIXES) and off by default. Routine titles that
  match a configured prefix are hidden from the brief schedule section without
  affecting the underlying event. (DGN-323)
- Daily retro: content-experience keyword matching is now config-driven
  (RETRO_CONTENT_TITLE_KEYWORDS). Entries whose title matches a configured
  keyword generate the content-impressions question instead of the default
  productivity prompt. (DGN-326)
- Morning brief task-lane and Warg-section embeds propagated to the template.
  Timed task-kind blocks (e.g., work routine events) now appear in the
  schedule section alongside appointments. Domain-agent morning sections
  (like Warg's workout summary) are injected inline with icon rules and
  timezone-generic layout. (DGN-282, DGN-283)
- lifekit.sh path-resolution note pinned in all four bundle skills that
  invoke it. The note now clarifies that the helper should be resolved
  relative to the workspace root, not the skill directory. (DGN-321)

### Fixed
- diet-log: multi-item meal logging via --new is now documented with correct
  splitting semantics; user-language-only splitting message propagated to the
  template.
- memory-engine: rrf_score=None no longer causes a crash in search output;
  the field is now guarded and treated as zero for ranking.
- Mirror engine: abandoned-transition leak fixed. When a recurring event
  batch is replaced, the old batch is now swept for tombstone entries and the
  corresponding calendar events are cancelled, eliminating duplicate calendar
  entries. 22 regression checks added to the mirror test suite. (DGN-302)
- diet-log and workout-log: render interpreter chain now points to the shared
  render venv (~/dogany/.venvs/render) instead of the bridge venv. Fixes card
  rendering failures on fresh instances where matplotlib is absent from the
  bridge venv. Propagation completes the fix that was partially delivered in
  v1.2.0 and subsequently overwritten by a skills-bundle refresh. (DGN-194)

### Changed
- upstream-report skill rerouted for self-maintained repos: dogany-agent and
  claude-code-telegram proposals now go through an internal ticket + direct
  fix rather than a public GitHub issue. Public issues are reserved for
  third-party framework dependencies. (DGN-293 owner directive 2026-07-16)
- notify policy (DGN-273), routine notify verbset, and remind engine: routine
  events now support per-event notification preferences; silent routines
  receive no reminder pushes. Template and 55-test suite updated.
- Mirror productization (DGN-268): S1-S5 landed, covering config seam,
  display-tz default pin, bootstrap adopt-or-create guard, onboarding UX,
  Google-unified auth, delivery wiring, Linux parity, cron safety rails, and
  poll-cycle per-step exception isolation. Merge-gate final-grill items
  resolved.
- install: model-choice step revised so newly minted agents default to an
  appropriate model for their subscription tier. (DGN-281)
- Baseline agent definitions (baseline-editor, propagation-editor,
  release-closer) propagated to the template with routing pointers in
  AGENT.md. New agents minted from the template inherit the full baseline
  toolset. (DGN-181)
- Retro and brief live-ahead improvements absorbed from Ag into the template
  baseline. (DGN-261)

## [1.4.0] - 2026-07-11

### Added
- Agents now resume interrupted work automatically after a bridge restart.
  The post-restart health check scans open wip tickets and the session inbox
  and picks up where it left off without waiting for user input. (DGN-254)
- lifekit project verbs: project-list, project-add, and project-upd are now
  available as delegatable CLI verbs. Agents can read and update projects
  through the SDK layer without direct SQL or live Notion API calls, removing
  the last Notion runtime dependency from the weekly-review routine. (DGN-256)
- lifekit v6: recurrence engine, routine_projection, and routine_roller land
  on canonical. Schema migrates to user_version 6 via migration 006
  (routine_recurrence tables). Migration applies automatically on the next
  update run and is additive-only. (DGN-259)
- Mirror engine ships as a flag-gated optional module (MIRROR_MODULE=off by
  default). When off, the enqueue hook is a fully silent no-op -- no errors,
  no output, no side effects. Agents that do not use the mirror feature are
  unaffected. (DGN-259)
- Live-dashboard sync (DashboardSync) ships in the agent template baseline.
  New agents minted from the template now inherit dashboard.py and the bot
  lifecycle wiring that keeps a pinned console dashboard current. The
  dashboard_enabled flag activates on file presence; the feature is off until
  you place the dashboard config file.
- Install completion flow improved: the wizard now shows your bot handle,
  explains how to start your agent with the dogany launcher, prompts you to
  configure sleep-prevention (so the agent stays up when the laptop lid is
  closed), and shows a cron schedule summary so you know when scheduled
  routines will first fire. (DGN-250)

### Fixed
- project-add now checks for an existing same-title project before inserting (EXISTS/exit 3, --new to force); mirror-poll.sh and mirror-reconcile.sh now enforce the MIRROR_MODULE flag at runtime instead of comment-only. (DGN-260)
- update.sh now refuses to overwrite instance files whose version marker is
  ahead of the framework source. If you have applied a hotfix or run a
  cutting-edge build that has not yet shipped in a release, the next update
  will skip that file and warn you loudly instead of silently rolling it back.
  The guard applies per-file; unguarded files update normally. There is no
  --force override by design. (DGN-249)

### Security
- Vendored bridge re-pinned to a clean, reachable upstream SHA (feca63e).
  The previous pin pointed to a dangling pre-history-rewrite commit authored
  by a blocked identity; UPSTREAM.md was the only public pointer to that
  object. The pointer has been removed.

### Notes
- This release carries user_version 6 (schema 006). Existing user_version 5
  installs migrate automatically through the 006_routine_recurrence migration
  on the next update run.
- The mirror module is off by default and requires explicit opt-in
  (MIRROR_MODULE=on in lifekit.conf) plus gws (Google Workspace) credentials.
  No setup is required for agents that do not use it.
- agents/main/database/ contains a stale 1685-line lifekit.py snapshot that
  predates this release. It is not the canonical copy; the canonical is
  database/lifekit.py at repository root. Cleanup is tracked separately.

## [1.3.0] - 2026-07-10

### Added
- lifekit task CLI verbs: task-add, task-find, task-done, task-undone,
  task-reschedule, task-archive, task-overdue, task-done-between, and
  event-window. Task mutations are now fully delegatable from the agent
  to the SDK layer without direct SQL. (DGN-180)
- Schema migration 005: nullable mirror-bookkeeping columns added to the
  event table, user_version pinned to 5. Migration applies automatically
  on the next update run and is additive-only (no existing data touched).
  (DGN-180)

### Fixed
- Updating an existing installation no longer leaves BRIDGE_MODELS missing
  from the instance .env. update.sh now backfills any absent keys
  (idempotent, add-only -- existing values are never overwritten). This
  closes a 3-release known issue where pre-v1.1 installs remained on
  sonnet-only after update because the seeding added in v1.1 only applied
  to fresh installs. (DGN-246)
- Slash command list order in the Telegram command picker now reflects
  usage frequency: new, stop, model, usage, skills, resume, history,
  help. (DGN-248)

### Notes
- This release carries user_version 5 (schema 005) and the full task CLI
  surface. It is the version-precondition for domain-agent minting: the
  Warg pilot requires a tag that carries 005 / user_version 5.

## [1.2.1] - 2026-07-09

### Added
- Agents now receive the results of background autonomous-loop runs as live
  session turns, so the agent knows what happened without waiting for you to
  ask. Quiet runs (nothing actionable) are suppressed and do not generate a
  notification. (DGN-217)
- After a bridge restart completes, the newly resumed session automatically
  verifies that the bridge is healthy and reports back only if something looks
  wrong. Routine restarts are now silent end-to-end. (DGN-226)
- Release preflight tool: before any release ships, a diff of the live agent
  against the canonical template is run and every divergence must receive an
  explicit verdict. Unreviewed live fixes can no longer be silently overwritten
  by an update. (DGN-225)

### Fixed
- /usage fallback display had a missing opening bracket in the Live Rate-Limit
  label; restored. (DGN-203)
- update.sh and self-update.sh now consume published release tags instead of
  pulling from main HEAD. Installing an update can no longer silently deliver
  unreleased development commits. A DOGANY_UPDATE_CHANNEL=main escape hatch is
  available for development instances. (DGN-221)
- install.sh now pins a fresh clone to the latest release tag before the setup
  wizard runs, so new installations start from a stable baseline rather than
  an arbitrary main HEAD. (DGN-221)
- Appointments whose start time falls between midnight and 09:00 local time
  (KST) no longer appear one day early in the morning briefing. The root cause
  was a date-bucketing query that applied UTC date() to locally-stored
  timestamps; the unified event schema (shipped in this release) resolves it
  structurally. (DGN-179 / DGN-220)
- Event schema upgraded to user_version 4: the event_persons junction table
  (appointment participants) and the appt_find/appt_show facade are now fully
  rewritten over the unified event table. Appointment queries are timezone-aware
  end-to-end. Migration 004 applies automatically on the next update run.
  (DGN-179 verb-delta v2)
- Framework updates no longer revert the bridge's launchd label and agent
  prefix back to generic placeholders, which previously caused self-restart to
  target the wrong service. Both values are now mint-time placeholders that
  survive update.sh. (DGN-213)
- Outgoing file transfers that time out now retry twice and send a user-visible
  notice on final failure, instead of silently discarding the file. (DGN-218)
- The memory-search skill now enforces a gate before the agent may claim that a
  value is not recorded: the agent must search first. This closes a gap where
  the agent would ask the user for data that was already in its own consolidated
  memory. (DGN-223)
- lifekit: workout sessions are now returned correctly from load_card_data, and
  the hook-effective burn macro is applied so morning brief calorie targets
  reflect actual workout output. (DGN-193)
- Transient Telegram send timeouts (ReadTimeout from the Telegram API) now show
  a friendly retry message instead of a raw "Error: Timed out" error string.
  (DGN-063)
- Single .env generator: secrets (bot token, email password) now travel only
  via environment variables, never through process arguments, closing a
  potential credential exposure in process listings. (DGN-096)

### Changed
- DGN-220 (appt_find UTC date-shift hotfix) is closed as superseded: the
  structural fix in DGN-179 covers the same bug for all users on this release.

## [1.2.0] - 2026-07-08

### Added
- New `/usage` command in Telegram: shows your live Claude rate-limit status
  as ASCII progress bars (5-hour window, weekly limit, per-model) with a
  countdown to the next reset. Output is localized (ko/en) to match your
  agent's language setting. Use `--full` flag for the detailed cache report;
  the default is the compact live view only.
- Self-update routing: agents now have a documented `self-update` workflow
  that consumes a published framework release without triggering a new
  release. The `routines/self-update.sh` script ships in the template so
  every minted instance inherits it.

### Fixed
- update.sh now requires a minted instance config (`.instance.conf`) before
  running and shows a preflight confirmation prompt. Bare invocations that
  previously silently targeted the wrong directory are now blocked upfront.
- update.sh AGENT_LANG lookup is now guarded against silent death under
  `pipefail`: a missing key in `agent.conf` no longer kills the update with
  no error message.
- Self-restart completion notice propagated to the `.template` baseline, so
  newly minted agents send a proactive Telegram notice when a bridge restart
  completes (previously users had to ask).
- `cleanup-files` routine no longer exits with an error code when the outbox
  or tmp directory is non-empty. A `set -e` footgun in the conditional log
  path was treating a false branch as a non-zero exit, causing a spurious
  "ROUTINE FAILED" notification every day once files accumulated.
- Event 3-layer SDK (task + appointment unified under `event`) landed on
  canonical: schema DDL, Python data-access layer, and migration script
  (DGN-178/179 P0). Fixes cross-agent data arbitration and time-slot
  ownership for multi-agent deployments.

## [1.1.0] - 2026-07-07

### Added
- update.sh now refreshes the framework constitution (RULES.md) and core shared
  services on every update, with the same user-edit detection and backup contract
  as dogany-* skills. A services manifest controls the exact refresh list; your
  AGENT.md and USER.md are never touched.
- Browser automation skill (agent-browser, Vercel Labs) ships as a default-dormant
  bundle skill. The skill is inactive unless the user opts in during install.
- Install wizard step 4c: optional browser automation opt-in. Discloses the
  Chrome for Testing download size (~684 MB) and that the agent-browser CLI will
  be installed via npm. Default answer is No.
- When opted in, the skill is activated by creating a symlink from
  .claude/skills/agent-browser into the bundle directory after the agent is minted.
- The DOGANY_BROWSER=1 env knob enables the opt-in in dry-run and scripted mode.
- Cron/routine failure visibility: all scheduled routines now run through a
  cron-guard wrapper. When a job exits with a non-zero code, a push notification
  is sent with the label, exit code, and log tail (one notification per label per
  day; repeats are suppressed).
- Skill display-name layer: skills can now declare a user-facing display name in
  their SKILL.md frontmatter, which the agent uses in menus and confirmations
  instead of the raw skill directory name.
- task-update skill gains three new verbs: reschedule, archive, and overdue.
  Tasks are now owned by lifekit.py (no direct SQL in the skill script). A
  schema migration adds the archived_at column for soft-delete support.
- Installer now seeds BRIDGE_MODELS into the instance .env based on your Claude
  subscription tier, so the /model picker shows the correct model options from
  the first session.
- Opt-in remote version check: set DOGANY_VERSION_CHECK=1 in your instance .env
  to receive a one-line notice when a newer Dogany version is available. Off by
  default; no data is sent beyond a plain version fetch.

### Fixed
- Bridge turn-death safety net: when a conversation turn ends abnormally (e.g.
  laptop sleep mid-turn), the agent now sends a user-visible notice instead of
  silently discarding the message. Includes inbound download retry for interrupted
  file transfers.

## [1.0.5] - 2026-07-06

### Added
- Bridge watchdog: a lightweight monitor checks the bot's polling heartbeat
  every 2 minutes and restarts the service when it goes zombie (alive but
  deaf). Two-strike design absorbs laptop sleep/wake; restarts are
  rate-limited so a deeper failure never causes a restart storm.
- Windows support via WSL2 (preview): setup script, install guard, and docs.
- The installer now auto-installs missing prerequisites (Homebrew, Python
  3.11+, git, Claude Code CLI) after a single confirmation, instead of
  failing one by one with manual instructions.
- Heavy downloads now run at the START of the install wizard, and large
  model downloads show live progress (native progress bars or an elapsed
  heartbeat) instead of a silent, frozen-looking screen.
- The agent remembers your model choice: a new session starts with the model
  the last session actually used, with a safe fallback chain (settings files,
  then default) when the remembered value is missing or invalid.

### Fixed
- update.sh no longer resets your model choice, leaves stale service files,
  misregisters backups, races during file replacement, or strips executable
  permissions (the last one could silently kill scheduled routines like
  morning briefings).
- Outgoing messages are scrubbed of internal tool-call markup that could
  occasionally leak into chat text.
- Appointment logging now checks the target date for existing entries before
  registering, preventing duplicate appointments.

### Changed
- Agents now propose skill updates at task completion when they had to
  deviate from a documented procedure (skill-feedback gate).
- Output rules: agents describe results in your terms and no longer expose
  internal mechanics (script names, API calls) in chat.

## [1.0.4] - 2026-07-04

### Added
- Onboarding now asks what you want your agent to be: a general life
  assistant or an agent with a specific role you describe.
- Your answer seeds the agent's primary focus in plain prose, so the
  agent starts out already oriented toward what you hired it for.

## [1.0.3] - 2026-07-04

### Added
- Arrow-key selection menus in the install wizard -- pick options with
  the arrow keys instead of typing numbers.
- Machine-aware model recommendations: the installer checks your RAM
  and free disk and recommends local models (embeddings, speech-to-text)
  that actually fit your machine.
- Claude token liveness check during install: a dead or invalid token
  is caught before setup finishes, not after.

### Fixed
- Reinstall guard: a stale marker from a deleted or moved installation
  no longer blocks a fresh install (it now self-heals).
- Removed outdated tier wording from installer messages.

## [1.0.2] - 2026-07-04

### Added
- New `dogany` command-line launcher for starting and managing your agent.

### Fixed
- Install wizard fixes from real-world install testing.
- Consistent generic labels in photo/voice message prompts (Korean).

### Changed
- Setup docs now recommend cloning into your home folder and warn
  against macOS-protected folders (Documents/Desktop/Downloads).

## [1.0.1] - 2026-07-03

### Added
- Optional semantic-memory step in the installer: install Ollama with
  the bge-m3 embedding model (~1.2GB) for cross-lingual memory recall,
  or skip it and use keyword search only.
- Manual timezone input is now validated (with retries) during install.

### Fixed
- Scheduled routines now convert your local times to the system clock,
  so they fire at the right time on servers set to a different timezone.
- Documentation now accurately describes uptime behavior and the
  semantic-recall dependency.

### Changed
- Refreshed Korean translations across the install and chat experience.

## [1.0.0] - 2026-07-03

Initial public release.

- A personal AI agent that lives on your machine and talks to you over
  Telegram, powered by Claude.
- Long-term memory: nightly consolidation of conversations plus
  semantic recall, so the agent remembers what matters to you.
- A skill system the agent uses (and extends) to do real work:
  reminders, scheduled routines, proactive messages, file handling.
- Optional life-management bundle (diet, workout, appointments,
  morning brief, daily retro) you can switch on conversationally.
- Guided installer with English and Korean support, from clone to a
  running agent in one session.
- Licensed under Apache-2.0.
