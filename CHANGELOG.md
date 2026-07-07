# Changelog

All notable user-facing changes to Dogany are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
