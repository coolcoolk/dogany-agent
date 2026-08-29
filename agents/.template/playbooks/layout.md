# Your agent lives here

This folder IS your agent -- its code, its memory, and all of your data.
Everything below is inside this one directory.

## Yours (edit freely, this is your data)
- `memories/` -- long-term memory (markdown notes)
- `files/` -- your inbox / outbox / scratch files
- `database/lifekit.db` -- your structured life data
- `identity/hot.custom.agent.md` -- the agent's identity
- `identity/hot.custom.owner.md` -- your profile
- `config/*.conf` -- your settings

## Do not touch (framework internals)
- `bridge/` -- the Telegram <-> Claude bridge
- `memory-engine/` -- the memory indexer/search code
- `rules/` -- framework rules + their rationale (refreshed by updates)
- `vendors/` -- channel contracts the bridge injects per session
- `playbooks/` -- framework how-to guides (this file lives here)
- `.claude/` -- harness config and skills
- `.telegram_bot/` -- bot token and access control (secrets live here)

## Backup
To back up your agent and everything it knows, copy this whole folder.
Restoring is just copying it back.
