# dogany-agent

A self-contained, batteries-included framework for running a personal
Claude-powered agent over Telegram. Clone it, drop in a bot token, and you have
a persistent assistant with long-term memory, scheduled routines, and a skill
system — all resolving relative to the repo, with zero personal data baked in.

## What's inside

- **Identity (3-layer)** — `CLAUDE.md` (loader) imports `RULES.md` (immutable
  operating rules), `AGENT.md` (the agent's own identity — a blank onboarding
  skeleton by default), and `USER.md` (the owner's profile — blank by default).
- **`bridge/`** — a self-contained Telegram <-> Claude bridge built on the
  official `claude-agent-sdk`. Vendored in-tree so the repo runs after a plain
  `git clone` (see `bridge/UPSTREAM.md`). The Python venv is NOT shipped.
- **`memory/`** — `memory.py`, the long-term recall + consolidation engine
  (markdown vault as source of truth, regenerable SQLite/FTS + embedding index),
  plus `CONSOLIDATION_TAXONOMY.md`. The index `state.db` is regenerated on first
  run.
- **`database/`** — `lifekit.py` / `lifekit.sh`, an optional structured-data lane
  (a local SQLite "life OS": meals, workouts, people, appointments). CODE ONLY —
  `schema.sql` is the structure; no `*.db` data is shipped. If a `lifekit.db`
  exists, the memory hook injects a canonical body-state line each turn;
  otherwise it cleanly no-ops.
- **`.claude/settings.json`** — Claude Code hooks: SessionStart recap +
  onboarding check, UserPromptSubmit memory recall, PreToolUse token-gate.
- **`.claude/skills/`** — framework skills (memory, cron-register,
  proactive-push, skill-creator, user-onboarding, appointment-log, relationship,
  task-update).
- **`routines/`** — nightly consolidation, weekly review, proactive push, file
  cleanup, plus their launchd plists.
- **`memories/`** — an empty `MEMORY.md` scaffold only (no real data).
- **`worklog/`** — a ticket template for tracking dev/infra work.

## Path independence

Everything resolves relative to the repo root, not a fixed parent tree:

- `PROJECT_ROOT` is the repo root. The bridge reads it from the environment; the
  launchd plists and hooks use `__PROJECT_ROOT__` placeholders substituted at
  setup time.
- The memory body-state hook finds lifekit via `LIFEKIT_DIR` env, then
  `PROJECT_ROOT/database`, then `<repo>/database` — and no-ops if none has a
  `lifekit.py`.
- `lifekit.sh` runs with any `python3` on PATH (override via `LIFEKIT_PYTHON`);
  `lifekit.py` and its DB path are relative to the script's own directory.

## Setup

1. Copy the env template and add your bot token:

       cp .telegram_bot/.env.example .telegram_bot/.env
       # edit .telegram_bot/.env: TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS

2. Build the bridge venv and install deps:

       python3 -m venv bridge/venv
       bridge/venv/bin/pip install -r bridge/requirements.txt

3. Point Claude Code hooks at this repo by substituting `__PROJECT_ROOT__` in
   `.claude/settings.json` (and the plists) with the repo's absolute path.

4. First run triggers onboarding: `AGENT.md` is a blank skeleton, and the
   SessionStart onboarding check walks you through naming the agent and setting
   tone. `USER.md` fills in over time via the memory write path.

## Notes

- English/ASCII in code; markdown docs may be in the agent's working language.
- No secrets or personal data are committed; see `.gitignore`.
- The optional `lifekit` lane is inert until you create a `lifekit.db` from
  `database/schema.sql`.
