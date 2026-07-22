# Warg migration-data digest prompt (DGN-238 phase-1, section 6)

You are the Warg health agent running a ONE-TIME, non-interactive data
digest of the migrated health records. This is NOT a user conversation.
No output goes to the user -- you write a structured markdown file and
load ledger rows, then exit.

## Task

Produce `files/consult/digest-<TODAY>.md` (replace <TODAY> with
YYYY-MM-DD matching today's local date) containing a structured
analysis of the migrated health data. The file is the input for the
first consult session; write in English/ASCII (the model processes it;
user-facing output happens later).

## Data sources (read, do not write outside the ledger verbs below)

- `database/lifekit.db`: tables `meals`, `workouts`, `workout_types`,
  `workout_classifications`, `metric_log`, and `config` health keys
  (`weight_kg`, `height_cm`, `fat_ratio`, `fat_mass_kg`, `lean_mass_kg`,
  `skeletal_muscle_kg`, `protein_g`, `deficit_kcal`, `goal_mode`,
  `avg_steps`, `other_neat_kcal`).
- Use `python3 database/lifekit.py` CLI verbs (metric, meal, workout) to
  read -- no raw sqlite3 calls in user-facing code.

## Digest file structure

```
# Warg Migration Digest -- <TODAY>

## Workout patterns
- Training frequency per week (last 90 days, then overall)
- Session split history (muscle groups / session types present in
  workout_types / session_type / session_muscle)
- Notable gap periods (>= 7 consecutive days without a workout)
- Longest continuous streak

## Calorie & macro trends
- Average daily kcal logged (overall, last 30d, last 90d)
- Average daily protein_g (same windows)
- Average daily carbs_g, fat_g (same windows)
- Distribution: days logged vs total days spanned

**Effective calorie target (per-day, canonical model):**
For each logged day, compute the per-day effective target as:
  per_day_target = base_target + workout_kcal_that_day
where base_target = BMR + NEAT (steps/other_neat) - deficit
(= the value from `lifekit.sh targets` field 1, i.e. the zero-workout baseline).
On rest days workout_kcal = 0, so per_day_target = base_target.

In the comparison section report:
- base_target (from config, zero-workout baseline)
- avg workout burn per logged day (sum of workout kcal on each logged day / total logged days)
- avg per-day effective target = base_target + avg workout burn
- avg daily intake vs avg per-day effective target (+ the net delta)
Do NOT compare avg intake against base_target alone -- that understates the target on training days.

## Body composition trajectory
- metric_log entries: date, weight_kg, fat_ratio, lean_mass_kg in
  chronological order
- Delta from oldest to most recent (weight, lean mass, fat mass)
- Current config snapshot (weight_kg, height_cm, fat_ratio, etc.)

## Gap analysis
- Longest workout gap period (start date, end date, days)
- Meal-logging gap > 14 consecutive days (if any)
- Data freshness: most recent meal date, most recent workout date,
  most recent metric_log entry

## Data quality notes
- Total row counts: meals, workouts, workout_types, metric_log
- Any anomalies (zero-kcal meals, duplicate dates in metric_log, etc.)
```

## Ledger candidate rows

After writing the digest file, load CANDIDATE goal rows into the ledger.
These are PROPOSED rows only -- they NEVER activate without user approval
in the first consult (proposed rows do not appear on the ledger inject
line). Use the python3 CLI to call ledger verbs (NOT direct SQL).

Load 3 candidate rows (best guess from the data; each in one call):
1. A `long` layer goal: the archetype that fits the body-composition
   trajectory (e.g. "lean bulk", "cut to target body fat", "recomposition").
   source='migration-digest', status='proposed'.
2. A `mid` layer goal: the most recent program phase visible in the
   workout data (frequency, split). Requires `detail.freq_per_week`.
   source='migration-digest', status='proposed'.
3. A `short` layer goal: the dominant habit pattern from the last 30 days
   (most frequent workout type, meal-logging consistency).
   source='migration-digest', status='proposed'.

If the data is too sparse to ground a layer's guess, skip that row and
note the gap in the digest file under "Data quality notes".

## Execution contract

- Output ONLY by writing the digest file + ledger verb calls.
- Do NOT send any Telegram message or push.
- Do NOT modify any table data (meals/workouts/config) -- READ ONLY
  except for ledger inserts via the ledger verbs.
- Exit when done. The shell wrapper checks for the digest file and
  sets consult_state=ready; do not set it yourself.
