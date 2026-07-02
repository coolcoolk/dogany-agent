---
name: relationship
description: >-
  __USER_LABEL__이 사람·관계를 조회하거나 등록하거나 별명으로 찾을 때 발동. "이름이 뭐더라", "OO 누구야",
  "OO 관계에 추가해줘", 별명/닉네임으로 사람을 언급할 때. 공용 SQL판(lifekit.sh)으로 persons 관리.
  이 스킬은 lifekit.db 기반 미래 표준 SQL판이다.
---

# relationship

Manage persons in lifekit.db persons table via lifekit.sh CLI. SQL-based standard.

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> walk up from skill dir to find database/)
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
