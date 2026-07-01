# dogany-agent

A self-contained, batteries-included framework for running a personal
Claude-powered agent over Telegram. Run `install.sh`, drop in a bot token, and
you have a persistent assistant with long-term memory, scheduled routines, and a
skill system -- resolving relative to the instance, with zero personal data
baked in.

## Repo layout

The repo mirrors the proven multi-agent tree: shared code is hoisted to the
root, and each agent lives under `agents/`.

- **`agents/main/`** -- the reference agent. Its `CLAUDE.md` (loader) imports
  `RULES.md` (immutable operating rules, symlinked to the shared `rules/`),
  `AGENT.md` (the agent's own identity -- a blank onboarding skeleton by
  default), and `USER.md` (the owner's profile, symlinked to `rules/USER.md`,
  blank by default). It also carries `bridge/`, `memory/`, `memories/`,
  `routines/`, `files/`, `worklog/`, `.telegram_bot/`, and `.claude/`.
- **`agents/.template/`** -- the mint source. A placeholder-ized agent
  (`__PROJECT_ROOT__`, `__AGENT_NAME__`, etc.) with framework skills and
  `RULES.md` symlinked to the shared roots. `scripts/mint.sh` copies from here
  (dereferencing the symlinks) to stamp out a new self-contained agent.
- **`rules/`** -- shared, immutable `RULES.md` plus the `USER.md` scaffold and
  `USER.example.md`. Agents symlink these in.
- **`skills/`** -- framework skills shared across agents (cron-register,
  proactive-push, skill-creator, user-onboarding). Agents symlink them into
  their `.claude/skills/`. Domain skills (diet-log, workout-log, appointment-log,
  relationship, task-update) live as real dirs inside each agent's
  `.claude/skills/`.
- **`database/`** -- `lifekit.py` / `lifekit.sh`, an optional structured-data
  lane (a local SQLite "life OS": meals, workouts, people, appointments). CODE
  ONLY -- `schema.sql` is the structure; no `*.db` data is shipped.
- **`service/`** -- a stable SDK facade (`service.lifekit`) over the lifekit
  core; skills import this rather than the raw data layer.
- **`scripts/`** -- `mint.sh`, which instantiates a standalone agent from
  `agents/.template` + the shared roots.
- **`install.sh`** -- a bilingual (ko/en) setup wizard: checks prerequisites,
  collects a bot token + owner id (born-locked), and calls `scripts/mint.sh` to
  mint a single self-contained instance, then optionally installs an autostart
  service.

Each agent's `bridge/` is a self-contained Telegram <-> Claude bridge built on
the official `claude-agent-sdk` (vendored in-tree; see `bridge/UPSTREAM.md`; the
Python venv is NOT shipped). The `.claude/settings.json` wires Claude Code hooks:
SessionStart recap + onboarding check, UserPromptSubmit memory recall, PreToolUse
token-gate, PostToolUse card follow-up.

## Path independence

Nothing assumes a fixed parent tree.

- A minted instance IS its own `PROJECT_ROOT`: it carries real copies (never
  symlinks) of the rules, framework skills, database schema, and service SDK.
- The bridge reads `PROJECT_ROOT` from the environment (set by `start.sh` via
  the launchd plist); the plists and hooks use `__PROJECT_ROOT__` placeholders
  substituted at mint time.
- `lifekit.sh` runs with any `python3` on PATH (override via `LIFEKIT_PYTHON`);
  `lifekit.py` and its DB path resolve relative to the script's own directory.
- The `service.lifekit` facade resolves the lifekit core from its own location
  (`service/lifekit/__init__.py` -> `../../database/lifekit.py`).

## Setup

Run the wizard from the repo root:

    bash install.sh

It walks you through language, timezone, bot token + owner id, then mints a
single self-contained agent (default `~/dogany-agent-instance`) and optionally
installs an autostart service. To preview without touching anything:

    bash install.sh --dry-run --lang en

To mint manually into a chosen directory:

    bash scripts/mint.sh --root /path/to/instance --name myagent

First run triggers onboarding: `AGENT.md` is a blank skeleton, and the
SessionStart onboarding check walks you through naming the agent and setting
tone. `USER.md` fills in over time via the memory write path.

## Notes

- English/ASCII in code; markdown docs may be in the agent's working language.
- No secrets or personal data are committed; see `.gitignore`.
- The optional `lifekit` lane is initialized from `database/schema.sql` at mint
  time (empty structured lane; no user data seeded).
