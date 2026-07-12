# 007 notify-policy cutover runbook

Migration 007 adds notify columns to `routine_def` and `event`. This file
documents the manual steps required after applying the migration to a live
instance that already has materialized routine instances.

## Background

`routine update notify=<policy>` re-stamps only roller-produced, non-exception
(`rec_exception=0`) future instances. Hand-moved instances (`rec_exception=1`)
are excluded from regen by design (they carry user-shaped data that the engine
must not overwrite). Any live exception instance retains its previous
`notify_policy` (NULL = default behavior) even after a rule-level notify change.

Today's commute-style exception rows (rec_exception=1, settled_at IS NULL)
that need the new policy applied must be updated individually.

## Step 1 -- re-stamp exception instances after a notify policy change

Run for each routine whose notify policy changed:

```
# find all live exception instances for the routine
lifekit.sh event-window $(date +%F) $(date -v+28d +%F 2>/dev/null || date -d '+28 days' +%F) \
  | grep "rd:<DEF_ULID>"    # or filter by title

# apply the new policy to each exception instance ulid
lifekit.sh event-notify <event_ulid> <policy>
```

Where `<policy>` is `silent`, `start_only`, `default`, or `<lead-minutes>`.

Repeat for every exception instance that should reflect the new policy.
Verify with:
```
sqlite3 database/lifekit.db \
  "SELECT ulid, rec_date, rec_exception, notify_policy FROM event \
   WHERE recurrence_id='rd:<DEF_ULID>' AND settled_at IS NULL;"
```

## Step 2 -- MAJOR-2 lockstep checklist (Ag v7 cutover)

When Ag upgrades to v7 (007 migration applied), the following components must
update in lockstep to stay compatible:

| Component | Required change |
|---|---|
| Warg agent.conf | L1 (lifekit_version) pin: change 6 -> 7 |
| Warg vendored lifekit | Re-vendor from Ag v7 (or OSS tag v7) |
| mirror ALLOWED_USER_VERSIONS | Re-pin to `(7,)` after 24h settling period |

Do NOT advance any one of these independently -- they form an atomic group.
Settling period: leave ALLOWED_USER_VERSIONS as `(6, 7)` for 24h after Ag
goes live on v7, then tighten to `(7,)` once no v6 Warg nodes remain.

## Notes

- This runbook is reference material; it does not need to run during
  `update.sh` (update.sh handles the schema migration automatically).
- Exception instance re-stamp (Step 1) is a one-time operation per affected
  routine after the first notify policy change on a live instance.
