---
name: relationship
display_name: 인연 기록
description: >-
  Fires when __USER_LABEL__ looks up, registers, or finds a person/relationship by
  nickname. "what was their name", "who is OO", "add OO to my relationships", or
  mentioning a person by nickname/alias. Manages persons via the shared SQL edition
  (lifekit.sh). This skill is the lifekit.db-based future-standard SQL edition.
---

# relationship

Manage persons in lifekit.db persons table via lifekit.sh CLI. SQL-based standard.

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> `LKIT="${PROJECT_ROOT:-$(pwd)}/database/lifekit.sh"`; CWD must be workspace root)
- SoT: local `$PROJECT_ROOT/database/lifekit.db` (persons table)
- aliases: stored in persons.aliases column, comma-separated — native support (unlike Notion)

## lifekit.sh quick reference
```
person-find  <name_or_alias>                -> id<TAB>name<TAB>relation<TAB>aliases
person-add   <name> [relation] [aliases]    -> new person id
person-alias <id> <alias>                   -> add alias (dedup ignored)
```

## lookup procedure
1. `lifekit.sh person-find <name_or_alias>` (searches name + aliases together)
2. 1 match -> report that person's info
3. 0 matches -> ask __USER_LABEL__: "new person, or alias for existing?" (no guess-register)
4. multiple matches -> show candidates, ask __USER_LABEL__ to confirm

## register procedure
1. `lifekit.sh person-add <name> [relation] [aliases]`
2. alias to add -> `lifekit.sh person-alias <id> <alias>`
3. done -> report to __USER_LABEL__

## boundaries
- delete person -> confirm with __USER_LABEL__ first.
- no bold (asterisk). address = __USER_LABEL__.
- SoT = lifekit.db only. do not mix with other stores.
