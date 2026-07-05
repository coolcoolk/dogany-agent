# Changelog

All notable user-facing changes to Dogany are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
