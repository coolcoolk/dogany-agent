# Your agent lives here

This folder IS your agent -- its code, its memory, and all of your data.
Everything below is inside this one directory.

## Yours (edit freely, this is your data)
- `memories/` -- long-term memory (markdown notes)
- `files/` -- your inbox / outbox / scratch files; `files/docs/` holds filed documents, listed in `files/INDEX.md`
- `database/lifekit.db` -- your structured life data
- `data/` = agent-owned records (skill notes); never cleaned; backed up nightly with the DB dump
- `identity/hot.custom.agent.md` -- the agent's identity
- `identity/hot.custom.owner.md` -- your profile
- `config/*.conf` -- your settings
- `data/board-plugin/` -- your workbench board plugin (how: skill `dogany-workbench-plugin`; never patch `routines/status-footer.py`)

## Do not touch (framework internals)
- `bridge/` -- the Telegram <-> Claude bridge
- `memory-engine/` -- the memory indexer/search code
- `rules/` -- framework rules + their rationale (refreshed by updates)
- `vendors/` -- channel contracts the bridge injects per session
- `playbooks/` -- framework how-to guides (this file lives here)
- `routines/` incl. `routines/bundle/` -- framework scripts and kit/pack payload; your own board plugin goes in `data/board-plugin/`, not here
- `.claude/` -- harness config and skills
- `.telegram_bot/` -- bot token and access control (secrets live here)

## Routing: where a durable fact or work item goes
Read this before you write a durable fact or a work item (the D6 pre-write
pointer sends you here). Framework canon; the one home of these rules.
- files/: inbox(owner-sent, not yet handled), docs(filed documents; routines/lib/filing.py is the only writer of docs/ and files/INDEX.md), outbox(send), tmp(scratch, daily-clean via cleanup routine), _archive(backups).
- Filing a kept document (after handling it): run routines/lib/filing.py -- never hand-move, never park your own output in inbox. It names "<date> <title>.<ext>", adds the files/INDEX.md row, collapses hash duplicates, and creates the axis folder lazily (the axis is a kit slot via config/filing-axis.conf, e.g. lifekit life domains; no kit = flat files/docs/ + tags). Clear -> `file`. Unclear (2+ equal candidates) -> `hold` under a group, ask the owner once per group (`questions`, then `asked`), apply the answer with `resolve` -- it becomes that group's precedent; still unresolved 30 days after the ask -> the daily sweep places a best-guess (state auto). Sensitive -> `--private` (hidden from casual lists). Tell the user the relative folder ("files/docs/finance/"), never an absolute path. Find with `search` / `view` before any absence claim.
- Route durable knowledge to its home: user fact -> identity/hot.custom.owner.md, agent identity -> identity/hot.custom.agent.md, reusable procedure -> its SKILL.md, complex artifact (program/doc) -> files/ as md. Everything else: the engine keeps memories/ automatically (nightly consolidate, weekly classify) -- do not hand-write memories/.
- Work-items (backlog, parked, tasks, pending decisions) belong to the ticket surface: status/priority/lifecycle is their system of record.
- For ticket status use `devkit_ticket set <ticket> --status <state>`; for progress use `devkit_ticket set <ticket> --progress <event> [--ref <reference>]` with the installed crew/lane options. Do not hand-edit status or append progress to Log. Typed events are started, done, blocked, unblocked, commit, tests, dispatch; blocked/unblocked, commit, tests and dispatch require their typed reference. Free-text Log or scope edits require citation re-judgment; a later writer call cannot bless an earlier hand edit.
- Not a durable fact or work item (a scratch file, a code edit, an answer for this turn): ignore this section.

## Backup
To back up your agent and everything it knows, copy this whole folder.
Restoring is just copying it back.
