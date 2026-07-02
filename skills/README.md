# skills/ -- shared framework skills

This folder holds the framework (default) skills shared across agents. Each
agent references them from its own `.claude/skills/` via a symlink, so fixing a
skill here updates every agent at once (no copies).

## Skill layers

- Framework skills (this folder): `dogany-cron-register`, `dogany-proactive-push`,
  `dogany-skill-creator`, `dogany-user-onboarding`, `dogany-memory-search`,
  `dogany-reminder`, `dogany-lifekit-setup`. Symlinked into every agent's
  `.claude/skills/`.
- Lifekit bundle skills: real directories in the template's
  `.claude/skills-bundle/<id>/` (`task-update`, `diet-log`, `workout-log`,
  `appointment-log`, `relationship`), DORMANT by default. The bundle is defined
  in `service/lifekit/bundle.conf` (primary source, includes the morning-brief /
  daily-retro routines). Activation is per instance and conversational: the
  `dogany-lifekit-setup` skill creates `.claude/skills/<id>` ->
  `../skills-bundle/<id>` symlinks and schedules routines via
  `routines/lib/routine-ctl.sh`. NEVER pre-place those activation symlinks in
  the template -- mint's `rsync -aL` would dereference them into permanent real
  dirs and break the off-toggle.
- Domain skills: real directories inside a specific agent's `.claude/skills/`
  (not symlinks). They belong to that agent instance only.

## env / tokens

Framework skill scripts never hardcode tokens, chat ids, or API keys. They read
them from the agent instance's `.env`, resolving the path relative to the script
location (`<instance>/.telegram_bot/.env`), so the same shared script works with
each agent's own bot.

## Activation

Skills activate two ways: runtime auto-discovery (the Skill tool matches a
skill's description) and the trigger rules an agent keeps in its `CLAUDE.md` /
`RULES.md`.
