---
name: appointment-log
description: >-
  Fires when __USER_LABEL__ makes, looks up, or edits an appointment/schedule, or
  handles its participants. "I made plans with X", "I'm meeting so-and-so", "what
  appointments do I have this week (today/tomorrow/this weekend)", "add someone to
  that appointment", "change the appointment's place/time", "dinner plans with OO",
  or mentions a person's name or nickname in an appointment context. Manages
  lifekit.db appointments and persons via the lifekit.sh CLI. The core is the
  "person-resolution rule": when attaching a person to an appointment, first look
  them up by real name or nickname, and if missing or ambiguous, confirm with
  __USER_LABEL__.
---

# appointment-log

Manage __USER_LABEL__ appointments in lifekit.db -- register, update, query, link persons.
All DB access via `lifekit.sh`. No raw SQL.

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> `LKIT="${PROJECT_ROOT:-$(pwd)}/database/lifekit.sh"`; CWD must be workspace root)
- SoT: local lifekit.db. Safe to edit directly.
- timezone: always __USER_LABEL__ local (Asia/Seoul, +09:00). Display times shown in local time.

## time input shapes

Two shapes. Shape detection is regex-first (classifier runs before any parse).

- date-only `YYYY-MM-DD` -> all-day appointment. single date = one day; two dates = multi-day span.
- datetime `YYYY-MM-DDThh:mm[:ss[.fff]][Z|+hh:mm]` -> timed appointment. naive datetime (no offset) = Asia/Seoul. ISO with offset or milliseconds accepted.

Mixed shapes (date-only start + datetime end, or vice versa) -> loud error, zero mutation.

## all-day vs timed behavior

- all-day: schedule_kind='all_day'. default slot_exclusive=0 (containing context, e.g. trip/travel). Does NOT block the day for timed appointments.
- timed: schedule_kind='timed'. slot_exclusive=1 (occupies the slot).
- end omitted on timed add: zero-length appointment (start=end). No slot protection window for that no-end case -- if meeting has a known duration, always give an end.

## person-resolution rule (follow every time a person is linked)

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
Never guess-create. Even 0 matches may be alias or duplicate -- must confirm.

## register / update procedure

0. DEDUP GATE -- run FIRST, before asking __USER_LABEL__ anything (even a
   missing start time): `appt-find <date> <date_plus_1>` for the target date.
   - existing appointment(s) on that date -> show them, ask via [[OPTIONS]]:
     merge/update into the existing one, or register as a separate new one?
   - proceed to appt-add ONLY on 0 matches or an explicit "new" answer.
1. parse time: expressions like "tomorrow evening 7pm" -> ISO relative to now (Asia/Seoul).
   start_at required.
   - timed: end omitted -> zero-length (start=end). Give end when duration is known.
   - all-day: date-only start, date-only end optional (multi-day); no time component.
2. `lifekit.sh appt-add <title> <start> [end location purpose summary]` -> new id.
3. link persons: apply person-resolution rule per person -> `appt-person <appt_id> <person_id>`.
4. partial update: `appt-upd <id> field=value ...` (only specified fields change).
   - time fields and metadata fields both accepted in one call (time applied first, then meta).
   - all-day row + date-only time value: start_at only = duration-preserving shift (whole range moves, length preserved); end_at only = keep start, extend/shrink from given end date; both = each endpoint from its own date. Reversed result -> loud error.
   - shape mismatch (datetime on all-day row, or date-only on timed row) -> loud error; use the SDK transition verb to change schedule_kind (not available via appt-upd).
5. done -> `appt-show <id>` to verify, then report summary to __USER_LABEL__.

## CLI quick reference (lifekit.sh)

```
person-find  <name_or_alias>                         -> id  name  relation  aliases
person-add   <name> [relation] [aliases]             -> new person id
person-alias <id> <alias>                            -> add alias (dedup ignored)
appt-find    <date_from> [date_to]                   -> appointments in range (local time)
appt-add     <title> <start> [end loc purpose summary] -> new appt id
appt-upd     <id> field=value ...                    -> partial update
            (field: title start_at end_at location location_url purpose summary)
appt-person  <appt_id> <person_id>                   -> link person to appointment
appt-show    <id>                                    -> 1 appointment + all persons (local time)
```

Empty positional arg -> hold place with "". e.g. `appt-add "AI Day" "2026-07-05T12:00:00+09:00" "" "venue"`

Wrong-id class: task id passed to appt-upd / appt-person / appt-show -> rejected with loud error, zero mutation.

## errors (loud, non-zero exit)

- mixed shapes (date-only + datetime) -> error, zero mutation.
- malformed time arg -> error.
- slot occupied (timed exclusive conflict) -> error.
- reversed interval (end < start) -> error.
- shape mismatch on upd -> error naming the issue; schedule_kind transition not available via appt-upd.
- wrong kind (task id) -> error.
- unknown person_id -> error.
- empty title= -> error (title cannot be empty).

## query response

- "this week's appointments" style -> exclude recurring work/commute blocks; report personal appointments only.
- appt-find and appt-show display times in local time (Asia/Seoul). timed -> local ISO with offset; all-day -> local date YYYY-MM-DD.
- cancelled appointments (abandoned) do not appear in appt-find results.
- tabular/sorted data -> code block (monospace). no bold (asterisk). address = __USER_LABEL__.

## boundaries

- Google Calendar sync needed -> confirm with __USER_LABEL__ separately (this skill = local DB only).
- appointment delete is hard to reverse -> confirm with __USER_LABEL__ first.
