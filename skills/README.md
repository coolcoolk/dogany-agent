# skills/ -- shared framework skills

This folder holds the framework (default) skills shared across agents. Each
agent references them from its own `.claude/skills/` via a symlink, so fixing a
skill here updates every agent at once (no copies).

## Framework vs domain skills

- Framework skills (this folder): `dogany-cron-register`, `dogany-proactive-push`,
  `dogany-skill-creator`, `dogany-user-onboarding`, `dogany-memory-search`, `dogany-reminder`. Symlinked into every agent's
  `.claude/skills/`.
- Domain skills: real directories inside a specific agent's `.claude/skills/`
  (not symlinks). They belong to that agent -- e.g. `diet-log`, `workout-log`,
  `appointment-log`, `relationship`, `task-update`.

## env / tokens

Framework skill scripts never hardcode tokens, chat ids, or API keys. They read
them from the agent instance's `.env`, resolving the path relative to the script
location (`<instance>/.telegram_bot/.env`), so the same shared script works with
each agent's own bot.

## Activation

Skills activate two ways: runtime auto-discovery (the Skill tool matches a
skill's description) and the trigger rules an agent keeps in its `CLAUDE.md` /
`RULES.md`.
