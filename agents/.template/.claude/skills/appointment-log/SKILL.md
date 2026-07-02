---
name: appointment-log
description: >-
  Fires when __USER_LABEL__ makes, looks up, or edits an appointment/schedule, or
  handles its participants. "I made plans with X", "I'm meeting so-and-so", "what
  appointments do I have this week (today/tomorrow/this weekend)", "add someone to
  that appointment", "change the appointment's place/time", "dinner plans with OO",
  or mentions a person's name or nickname in an appointment context. Manages
  lifekit.db appointments / persons / appointment_persons directly via the lifekit.sh
  CLI. The core is the "person-resolution rule": when attaching a person to an
  appointment, first look them up by real name or nickname, and if missing or
  ambiguous, confirm with __USER_LABEL__.
---

# appointment-log

Manage __USER_LABEL__ appointments in lifekit.db — register, update, query, link persons.
All DB access via `lifekit.sh` (= lifekit.py core). No raw SQL.

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> walk up from skill dir to find database/)
- SoT: local lifekit.db. No Notion sync (notion_id = legacy import artifact). Safe to edit directly.
- timezone: always __USER_LABEL__ GMT+9 (+09:00). date = YYYY-MM-DD, start/end = ISO (+09:00) preferred.

## data schema
- `appointments`: id, title, start_at, end_at, location, location_url, purpose, summary
- `persons`: id, name, relation, aliases (comma-joined), birthday, job ...
- `appointment_persons`: (appointment_id, person_id) N:M join

## person-resolution rule (follow this order every time a person is linked)
__USER_LABEL__ mentions someone (name or alias) -> find first, never register blind.

```
1. lifekit.sh person-find <name_or_alias>   (searches name + aliases together)
2. exactly 1 match   -> link directly (appt-person)
3. 0 matches         -> ask __USER_LABEL__:
      "Is OO an alias for someone existing, or a new person?"
      - existing alias -> person-alias <id> <alias> -> link
      - new person     -> person-add <name> [relation] [aliases] -> link
4. 2+ matches        -> show candidates, ask __USER_LABEL__ to pick, then link
```
Ask using [[OPTIONS]] format (numbered list, marker on last line).
Never guess-create. Even 0 matches may be alias or duplicate — must confirm.

## register / update procedure
1. parse time: expressions like "내일 저녁 7시" -> ISO relative to now (GMT+9).
   start_at required. end_at omitted -> appt-add defaults to start+3h
   (__USER_LABEL__ gives end/duration -> use that value).
2. `lifekit.sh appt-add <title> <start_at> [end_at location purpose summary]` -> new id.
3. link persons: apply person-resolution rule per person -> `appt-person <appt_id> <person_id>`.
4. partial update: `appt-upd <id> field=value ...` (only specified fields change).
5. done -> `appt-show <id>` to verify, then report summary to __USER_LABEL__.

## CLI quick reference (lifekit.sh)
```
person-find  <name_or_alias>                        -> id  name  relation  aliases
person-add   <name> [relation] [aliases]            -> new person id
person-alias <id> <alias>                           -> add alias (dedup ignored)
appt-find    <date_from> [date_to]                  -> appointments in range
appt-add     <title> <start_at> [end_at loc purpose summary] -> new appt id
appt-upd     <id> field=value ...                   -> partial update
            (field: title start_at end_at location location_url purpose summary)
appt-person  <appt_id> <person_id>                  -> link person to appointment
appt-show    <id>                                   -> 1 appointment + all persons
```
Empty arg -> hold place with "". e.g. `appt-add "AI Day" "2026-07-05T12:00:00+09:00" "" "우리집"`

## query response
- "이번 주 약속" style -> exclude recurring work/commute blocks; report personal appointments and tasks only.
- tabular/sorted data -> code block (monospace). no bold (asterisk). address = __USER_LABEL__.

## boundaries
- Google Calendar sync needed -> confirm with __USER_LABEL__ separately (this skill = local DB only).
- appointment delete is hard to reverse -> confirm with __USER_LABEL__ first.
