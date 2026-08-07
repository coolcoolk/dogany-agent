---
name: dogany-routine
display_name: 생활 루틴
description: >-
  Register, edit, list, pause/resume, and retire recurring routines in the
  lifekit ledger. Fires when __USER_LABEL__ says things like "register a
  daily 6am run", "set up a Mon/Wed/Fri morning workout", "remind me to take
  supplements every other day", "add weekday morning stretching", "add/make a
  routine", "show my routines", "what routines do I have", "change the
  routine time", "adjust the frequency", "stop/retire this routine",
  "pause/resume the routine", "when is my routine this week", "show routine
  adherence/health". Handles register (cadence parse + meta inference +
  notification question + question-form confirm), edit (rule change vs
  this-instance-only exception), list/status, health query, retire. Writes
  through lifekit.sh routine verbs only (SoT=sqlite; mirror rides the
  outbox). Final output = registration echo with this week's occurrence
  dates.
---

# dogany-routine -- recurring routine register / edit / retire

Own the conversation flow for routine_def lifecycle (DGN-240). NEVER write
event rows by hand -- every write goes through `lifekit.sh routine <verb>`.
Ledger = sqlite (SoT). Mirror surfaces update themselves via outbox.

## flow: register (8.2)

1. parse cadence from utterance. deterministic table:
   - "every day" / "daily"                -> D
   - "weekdays"                           -> W:MON,TUE,WED,THU,FRI
   - "weekends"                           -> W:SAT,SUN
   - weekday list ("Mon/Wed/Fri")         -> W:MON,WED,FRI style
   - "every other day"                    -> I:2@<first date>
   - "every N days"                       -> I:N@<first date>
     (first = explicit date in utterance, else today)
   - time mentioned -> timed, time=HH:MM (+ duration; unstated = 30min)
   - no time mentioned -> all_day
2. infer meta. NO interrogation (question budget: ONE confirm question total,
   notify included):
   - area: keyword match vs areas table (`sqlite3` read or lifekit).
   - project: fuzzy match title/context vs projects. not confident -> NULL.
   - purpose: goal phrase in utterance or context. none -> ask in confirm.
3. confirm -- ONE QUESTION total (covers inferred values + notify policy).
   Facts the user said = restate. Inferences = ask, never assert.
   Always include the notify line in the confirm, stating the default or
   echoing what the user already said, and inviting adjustment:
     "Adding '<title>' <cadence> at <time> in <area>
      -- with default alerts 30 min before + on-time. Keep alerts, or
      adjust? (silent / start-time only / N min before)"
   If the user stated a notify preference in the utterance, restate it here
   instead of the default (do NOT ask again -- 1 question budget is firm).
   Map the confirm answer to the notify field:
   - keep default            -> omit notify (or notify=default)
   - no alerts               -> notify=silent
   - start-time alert only   -> notify=start_only
   - "N minutes before"      -> notify=N   (lead alert + on-time alert kept)
4. write after OK:
   `lifekit.sh routine add "<title>" "<cadence>" time=HH:MM duration=N
    area=<name> project=<title> purpose="<one line>" [end=YYYY-MM-DD]
    [notify=default|silent|start_only|<lead-min>]`
   - exit 3 + "EXISTS n" = same-title active routine exists -> show it, ask
     ONE question: modify existing or force new (`--new`).
   - success line returns materialized dates -> reply with THIS WEEK's
     occurrence dates.
   - all_day registers today too; timed registers today only if the time is
     still ahead (engine rule -- do not restate wrongly).

## flow: edit -- two modes ONLY (spec 4)

Ask which the user means when ambiguous (1 question):
- this instance only ("just push tomorrow's back") ->
  `lifekit.sh routine exception <event_ulid> date=YYYY-MM-DD` (all_day move)
  or `start_at=... end_at=...` (timed move) or `title=/note=`.
  Find the instance ulid via `lifekit.sh event-window` or task-find.
- rule change ("from next week make it 7am") ->
  `lifekit.sh routine update <def_ulid> time=07:00
   [effective_from=YYYY-MM-DD]`
  - conversation edit: omit effective_from (= today).
  - retro decision: effective_from = next Monday.

### notify edit (DGN-273)
- change the routine's alerts ("mute this routine", "alert me 10 min
  before instead") ->
  `lifekit.sh routine update <def_ulid> notify=silent|start_only|<lead-min>`
  (notify= with an empty value resets to default).
  The engine re-stamps ONLY roller-produced, non-exception future instances.
  Hand-moved instances (rec_exception=1) are NOT re-stamped automatically --
  list them with `lifekit.sh event-window <today> <+28d>` and apply the new
  policy per instance: `lifekit.sh event-notify <event_ulid> <policy>`.
- one instance only ("no alert for tomorrow's") ->
  `lifekit.sh event-notify <event_ulid> silent` (also takes
  default|start_only|<lead-min>|'' to reset). Works on one-off tasks and
  appointments too.

## flow: lifecycle

- pause: `lifekit.sh routine pause <def_ulid>` (future instances withdrawn)
- resume: `lifekit.sh routine resume <def_ulid>` (window refreshed +
  re-materialized)
- retire: `lifekit.sh routine retire <def_ulid>` -- ALL future occurrences
  cancelled incl hand-moved ones. Confirm before retiring (destructive-ish,
  reversible only by re-registering).

## flow: query

- list: `lifekit.sh routine list [active|paused|retired]`
- detail: `lifekit.sh routine show <def_ulid>` (includes notify_policy /
  notify_lead_min)
- health (adherence): `lifekit.sh routine health [window=28]` -- JSON per
  routine (done/missed/skipped/rate/anomaly). Weekly review consumes this;
  ad-hoc asks get a short human summary, not raw JSON.

## failure modes (8.3)

- cadence unparseable -> ONE clarify question w/ 2 nearest readings.
- meta unresolved -> ride the confirm question; still unresolved ->
  register with NULL, retro backfills (no interrogation).
- duplicate title -> show existing def + 1 question (modify / new).
- bad notify value -> the verb rejects loudly; re-ask with the 4 options.

## bounds

- trigger tier: BEST-EFFORT (conversation skill; no guaranteed hook).
- never hand-edit lifekit.db / event rows; verbs only.
- never create surface (GCal/GTasks) entries directly; outbox owns mirror.
- kind is always task in v1; appointment-shaped routines are refused by the
  verb (say so and offer a plain repeating appointment discussion instead).
