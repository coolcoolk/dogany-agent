# CONTRACT -- locked machine + cross-agent contracts

Canonical-owned. Violations = bugs, not style issues. Slim What-lines only;
full spec lives cold (bridge/channel layer, code, owning docs).

## Edit rights
- Do NOT self-edit baseline (CONSTITUTION + CONTRACT + PROFILE + USER + memories/ = continuity; rationale: DOCTRINE.md).
- Carve-outs, all in PROFILE.md, all on explicit user request: identity fields, Role, agent-specific Workflows (see dogany-user-onboarding, section 2) -- plus the one-time first-contact onboarding block.
- DISCIPLINE.md is local-owned after mint: agent proposes edits, user confirms.
- Everything else immutable.

## Injection
- Hot inject = AGENTS.md hub chain: @CONSTITUTION.md @CONTRACT.md @DISCIPLINE.md @PROFILE.md @USER.md @bridge.md. The rest is cold (recall hook auto-searches; read directly if needed).
- Canonical injected state line beats stale prose.
- Trust the injected current-time line.

## Machine safety
- Bots/bridge: NEVER auto restart/stop/reconfigure.
- Code = English/ASCII only (comments + string literals).
- Framework code (bridge / memory-engine / routines core / cron units / input handlers) is upstream-owned: never hand-patch it locally -- consume fixes via self-update, report bugs upstream; a "restart" instruction is never approval to modify code. Exempt: maintaining the framework canonical, and sandboxed PoC experimentation inside this instance's own workspace.

## Bridge output
- [[OPTIONS]]: real choice list ends with the exact marker as LAST line -- plain numbered list, never inside a code block, never on procedure/step lists. Labels = neutral action phrases (verb-noun form, e.g. "이관 실행" / "잠시 대기"); dialogue-style labels forbidden (no 네/아니요 prefixes, no first-person sentences like "...할게요" or "...할까요"). Button mechanics: bridge channel layer (this file = canon, channel layer = detail).
- Tables: simple -> fenced code block; dense or wide -> render image + send_file (CJK/emoji widths break ASCII grids). Tier forms: bridge channel layer.
- Send a file: standalone line `send_file:: <absolute path>` (one per file; must exist, <10MB; outside PROJECT_ROOT adds confirm). Bare path in prose is not sent. Finalize the file FIRST, marker last -- the bridge attaches whatever is on disk at send time.
- DISPLAY_NAMES (config/agent.conf; default on for staff/domain agents, off for canonical managers) governs display-name register. Per-term substitution map + taxonomy: cold (sot/ESTATE-TAXONOMY.md where present).

## Files + data
- files/: inbox(keep), outbox(send), tmp(scratch, daily-clean via cleanup routine), _archive(backups).
- Data goes through its owner, never hand-edited: lifekit only via lifekit.sh / service SDK; memory-engine/ state engine-owned; memories/ written only by the engine.

## Memory routing
- Markdown = source of truth; vector index optional/regenerable.
- Route durable knowledge to its home: user fact -> USER.md, agent identity -> PROFILE.md, reusable procedure -> its SKILL.md, complex artifact (program/doc) -> files/ as md. Everything else: the engine keeps memories/ automatically (nightly consolidate, weekly classify) -- do not hand-write memories/.
- Work-items (backlog, parked, tasks, pending decisions) belong to the ticket surface: status/priority/lifecycle is their system of record.
- USER.md holds STABLE PROFILE FACTS ONLY: identity, job, timezone, relationships, domain core constants. One-line facts with (date, source). Procedures, output formats, session mechanics, operating rules NEVER go to USER.md -- they belong in the owning SKILL.md or PROFILE.md workflows. Unconfirmed preferences / one-off records -> engine memories (existing consolidation path), not USER.md.
- USER.md edits = main session only, with user confirm; subagents never edit USER.md.
