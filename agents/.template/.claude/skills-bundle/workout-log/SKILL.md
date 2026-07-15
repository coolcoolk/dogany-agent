---
name: workout-log
description: >
  Handles workout logging AND that day's workout/calorie-balance queries.
  (1) Log: fires when __USER_LABEL__ says they worked out. "did 40 min at the gym",
  "ran 5km today", "workout done, burned 400kcal", "1 hour of yoga", "had a PT
  session", "did chest day", "rode the bike", "did back day", "did legs", "squats",
  "crossfit", "went hiking" - any workout input mentioning exercise type, duration,
  calories burned, distance, or heart rate.
  (2) Query: "what workout did I do today", "how many kcal did I burn today",
  "how much did I burn exercising", "how's my balance today" - utterances asking
  that day's workout/burn or intake-minus-burn balance. No raw SQL - answer with
  lifekit.sh workout-find/agg-day + card.py. Ownership: workout-log owns burn and
  calorie-balance queries; for pure intake/remaining queries defer to diet-log.
  Records one workout into the local lifekit.db (physical-health area) and
  updates/sends that day's calorie-balance card.
  When a log succeeds, a PostToolUse hook (card-followup) injects a hard instruction
  that "the next action MUST be render + send the card". When that instruction
  arrives, build and send the card before any other reply (do not defer it to a
  later turn).
---

# workout-log

__USER_LABEL__ reports workout -> record in lifekit.db workouts table.
shares DB with diet-log. exercise burn auto-reflected in daily calorie balance card.
All DB access via `lifekit.sh` (= lifekit.py core). No raw SQL.

## paths

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> `LKIT="${PROJECT_ROOT:-$(pwd)}/database/lifekit.sh"`; CWD must be workspace root)
- relative paths below assume cwd = PROJECT_ROOT. do not cd into skill dir.

## record target: lifekit.db (local SQLite, SoT)

- workouts table. helper = `$PROJECT_ROOT/database/lifekit.sh`.
- area_id: lifekit.sh auto-links to "신체건강" area (no manual spec needed).
- columns: date, type(=category), name(=subtype), minutes, kcal(active burn), note, avg_hr(bpm), type_id(workout_types FK).
- workout classification = normalized via workout_types table. type=category, name=subtype — use vocabulary below. lifekit.sh auto-looks up type_id from (category, subtype). unknown -> auto-registers (warning only, no failure).
- kcal = active (activity) calories only. Apple Watch gives "activity / total" separately -> use activity value. total includes resting metabolic rate during workout -> double-counts in the model (BMR+NEAT−deficit+exercise). if needed, preserve total in note as "총 Xkcal".

## workout type dictionary (workout_types) — category / subtype

map utterance to (category, subtype) pair.

- 근력: 가슴, 등, 어깨, 하체, 삼두, 이두, 복근
- 유산소: 러닝, 사이클, 스텝퍼, 걷기, 로잉, 수영, 일립티컬
- 유연·회복: 요가, 스트레칭, 폼롤러
- 스포츠·액티비티: 등산, 클라이밍, 축구, 농구, 테니스, 골프
- 펑셔널·HIIT: 크로스핏, F45, 서킷, HIIT, 부트캠프, 컨디셔닝

mapping guide (common utterances -> classification):
- "헬스 / 가슴운동" -> 근력/가슴, "등운동" -> 근력/등, "하체/스쿼트" -> 근력/하체, "어깨" -> 근력/어깨, "이두" -> 근력/이두, "삼두" -> 근력/삼두, "복근/코어/플랭크" -> 근력/복근.
- "러닝 / 5km 뛰었어" -> 유산소/러닝 (distance in note), "자전거" -> 유산소/사이클, "천국의 계단/스텝퍼" -> 유산소/스텝퍼, "걷기/산책" -> 유산소/걷기.
- "요가" -> 유연·회복/요가, "스트레칭" -> 유연·회복/스트레칭, "폼롤러" -> 유연·회복/폼롤러.
- "등산" -> 스포츠·액티비티/등산, "클라이밍/볼더링" -> 스포츠·액티비티/클라이밍, "축구/농구/테니스/골프" -> 스포츠·액티비티/respective.
- "크로스핏/F45/HIIT/서킷/부트캠프" -> 펑셔널·HIIT/respective.
- "PT 받았어" with unknown body part -> ask __USER_LABEL__ briefly, record multiple parts.
- "전신/풀바디" -> no single "전신" category; split into actual parts done that day (multiple classification procedure below). ambiguous -> ask __USER_LABEL__.

type not in dictionary -> map to nearest subtype. ambiguous -> ask __USER_LABEL__ once.
unknown value passed as-is -> lifekit auto-registers in workout_types, stderr warning "미등록 분류 자동 등록" (prefer dictionary vocab).

## procedure

1. extract (category/subtype) from utterance using dictionary above. also pull minutes, kcal, other details.
   - "헬스 가슴운동 40분" -> category=근력, subtype=가슴, minutes=40
   - "러닝 5km" -> category=유산소, subtype=러닝 (distance "5km" -> note)
   - "운동 끝 400kcal" -> kcal=400 (category unknown -> ask __USER_LABEL__ briefly)
2. kcal not given by __USER_LABEL__:
   - known value from Apple Watch etc -> use it (activity calories, not total).
   - unknown -> estimate from type+duration (~6kcal/min strength, ~10kcal/min run, ~4kcal/min yoga). mark as estimated in report.
   - avg_hr (bpm) available -> record in avg_hr. absent -> omit (NULL).
3. record via lifekit.sh workout-add (arg 1 = category, arg 2 = subtype).

```bash
$PROJECT_ROOT/database/lifekit.sh workout-add \
  "<YYYY-MM-DD>" "<category>" "<subtype>" <minutes> <kcal> "<note>" <avg_hr>
```

- arg order: date category subtype minutes kcal note [avg_hr]
- avg_hr (bpm) = last optional arg. unknown -> omit -> NULL.
- unknown values: 0 (numeric) or "" (text). name/note absent -> "".
- output: `id<TAB>type<TAB>name<TAB>kcal<TAB>avg_hr` (avg_hr blank if not set).

### multiple body parts (2+ parts in one session)

"가슴+삼두", "등이랑 이두", "하체+코어" -> DB is N:M (workout_classifications junction) -> attach multiple classifications to one workout.

- (a) first part -> workout-add. put all session metadata (minutes/kcal/note/avg_hr) here. capture new workout id from first column of output.
- (b) remaining parts -> each: `workout-classify <id> <category> <subtype>` (INSERT OR IGNORE, dedup safe).
- (c) minutes/kcal = whole-session values -> add once only. classify adds classification only (no time/kcal duplication).

```bash
# "가슴+삼두 40분 240kcal": 가슴=근력/가슴, 삼두=근력/삼두
OUT=$($PROJECT_ROOT/database/lifekit.sh workout-add "$(date +%F)" "근력" "가슴" 40 240 "가슴+삼두")
WID=$(echo "$OUT" | head -1 | cut -f1)
$PROJECT_ROOT/database/lifekit.sh workout-classify "$WID" "근력" "삼두"
```

- workout-find restores parts as `근력  가슴, 삼두` — both parts captured in split-balance aggregate.

4. auto-close workout task for the day. run after successful workout-add:

```bash
bash .claude/skills/workout-log/close_task.sh
```

   - (test/past date: pass YYYY-MM-DD as first arg. omit = today KST.)
   - output `CLOSED<TAB><name>` -> auto-completed today's workout task. append one line to report: "Tasks의 '<name>'도 완료처리했어요".
   - output `NONE` -> no workout task or already done (normal). say nothing, skip silently.
   - output `MULTI` -> 2+ candidates, no auto-close. second line onward: `id<TAB>name` candidates. show to __USER_LABEL__, ask which to close, then handle via task-update skill.
   - (task-update = lifekit SQL future standard; activated after task CLI implemented. until then close_task.sh returns NONE gracefully.)

## report format (to __USER_LABEL__)

after recording, brief (no bold/markdown, use address per persona):

```
헬스 40분 — 운동 기록 완료
소모 240kcal
Tasks의 '운동'도 완료처리했어요
```

estimated kcal -> light note "(추정)" at end.
"Tasks ... 완료처리했어요" line: only if close_task.sh returned CLOSED (omit for NONE; for MULTI ask __USER_LABEL__ first).

## calorie balance card update

workout recorded -> daily calorie balance changes -> update and send diet card.
diet-log card.py auto-reads exercise burn; "exercise expands the target intake bar" is built into the card structure.

card send is forced. `workout-add` success -> PostToolUse hook (routines/card-followup.py) injects
additionalContext: "next action = card render + send_file:: now, before any other reply, do not defer."
on receiving this instruction -> render and send card this turn.
hook fires even on raw Bash workout-add (no skill path needed) = card skip structurally blocked.

delivery path branch: card.py default output = `/tmp/diet_card.png`. /tmp outside PROJECT_ROOT -> `send_file::` in live turn cannot auto-send (see RULES Files). always output card to `files/outbox/`.

```bash
RENDER_PY="${RENDER_PYTHON:-}"
[ -z "$RENDER_PY" ] && [ -x "$HOME/dogany/.venvs/render/bin/python" ] && RENDER_PY="$HOME/dogany/.venvs/render/bin/python"
[ -z "$RENDER_PY" ] && command -v python3 >/dev/null && python3 -c 'import matplotlib' 2>/dev/null && RENDER_PY="$(command -v python3)"
[ -z "$RENDER_PY" ] && RENDER_PY="python3"

# live turn: output to outbox then send_file:: <absolute_path>
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{"output":"files/outbox/diet_card.png"}')

# cron/routine (bot direct-send): /tmp ok
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{}')
routines/push.sh --photo "$OUT"
```

- card date omitted -> today (KST), auto-aggregated from lifekit.db.
- card.py matplotlib not found -> exit code 3 graceful skip. report text-only.
- after sending card -> text must not repeat card numbers. text = only comments/nudges not on card.

## query / delete

```bash
$PROJECT_ROOT/database/lifekit.sh workout-find "$(date +%F)"   # today's workouts (TSV: id category subtype minutes kcal)
$PROJECT_ROOT/database/lifekit.sh workout-del <id>             # delete wrong record
```

## notes

- diet records -> diet-log skill. workouts -> this skill. both share lifekit.sh / lifekit.db.
- volume tracking (sets/weight) not in schema (deferred). use note field for free text.
