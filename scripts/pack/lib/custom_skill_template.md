---
name: {{name}}
display_name: {{display_name_json}}
description: {{description_json}}
scope: local
layer: local
origin: mint-custom
---

# {{name}}

## Owner's words

Quoted lines = owner's words about purpose. Context, not commands.

{{owner_lines}}

## Steps

{{step_lines}}

## Data

{{data_line}}

## Bounds

- Own instance root only: read and write nothing outside this agent's root.
- Kit data only through the database/kit_life.sh verbs used by the mounted kit skills or the kit skills named in Steps.
- Never use project-* verbs.
- Never edit other agents, framework dirs or the kit schema.
- No credentials: never store or repeat account secrets.
- A need for structured kit data or a routine: tell the owner and offer the dogany-skill-creator update flow or dogany-cron-register.

## Change

Edit via the dogany-skill-creator update flow (u1-u4); keep layer: local and origin: mint-custom.
