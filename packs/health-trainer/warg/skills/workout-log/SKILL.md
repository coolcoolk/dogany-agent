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
  (3) Design/planning (DGN-229 design layer): "운동 프로그램 짜줘", "무슨 운동
  할까", "루틴 설계해줘", "분할 어떻게 나눠", "몇 세트 몇 회가 좋아", "크레아틴
  운동에 도움 돼", "운동 전에 뭐 먹을까", workout program design, routine
  planning, split design, volume/frequency question, ergogenic supplement
  question - any ask for training DESIGN rather than a log or status. Consult
  the kimwog knowledge warehouse (knowledge/kimwog, snapshot @ release pin)
  FIRST, refract items against the user ledger (goals/constraints/resources/
  preferences), respect grades/contested flags, numbers only via the skill
  path. Warehouse miss -> honest absence + gap log to
  knowledge/kimwog/GAPS-instance.md.
---

# workout-log

__USER_LABEL__ reports workout -> record in lifekit.db workouts table.
shares DB with diet-log. exercise burn auto-reflected in daily calorie balance card.
All DB access via `lifekit.sh` (= lifekit.py core). No raw SQL.

## paths

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> walk up from skill dir to find database/)
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

---

## design layer (DGN-229) -- training DESIGN, not logging

Fires on trigger class (3): 프로그램 짜기, 무슨 운동 할까, 루틴/분할 설계,
볼륨-빈도 질문, 운동 보충제 질문. Record half above stays unchanged; this
layer is the DESIGN half. Knowledge base = kimwog warehouse snapshot at
`knowledge/kimwog` (consumed @ release pin, see `knowledge/kimwog/.snapshot-pin`).
Refraction rules canonical at `knowledge/kimwog/REFRACTION.md` -- read it when
in doubt, do not restate from memory.

### 1. warehouse FIRST (before answering any design ask)

- items live in `knowledge/kimwog/items/*.md` (frontmatter: id, domain, grade
  A..E, lane, as_of, contested, sources, applicability.axes, refraction_notes).
  Training asks -> `exercise-*` items (lanes: @clinical-physio,
  @performance-lit, @gym-craft); recovery asks -> `sleep-recovery-*`;
  diet-side crossovers -> defer to diet-log design layer. Grep/read
  frontmatter by claim keywords.
- verify domain/item status BEFORE citing (alias resolution precedes status):

```bash
python3 knowledge/kimwog/tools/resolve.py --root knowledge/kimwog <domain-or-item-id>
# e.g. python3 knowledge/kimwog/tools/resolve.py --root knowledge/kimwog exercise/creatine-ergogenic-001
```

  render=active -> usable. render=retracted -> tell __USER_LABEL__ the knowledge
  was retracted, do not cite it as current. render=missing -> warehouse miss
  (section 4).
- filter by applicability: item `applicability.axes` + `lane` decide fit. axis
  values resolve measured (lifekit) > stated (ledger/profile) per REFRACTION.md.
- grades: D/E grade or `contested: yes` -> present WITH honesty markers
  ("근거 등급이 낮아요", "전문가 사이에 이견이 있어요"), never as settled fact.
  Know the @gym-craft caveat: warehouse #1 ships ZERO gym-craft items
  (GAPS.md) -- split/tempo/programming craft asks are warehouse misses today.
  NEVER bake a bare letter grade into prose (M-2: grades speak only via this
  skill path).
- record consumption (touched-set, enables release-diff re-query):

```bash
python3 knowledge/kimwog/instance/tools/touched.py --root knowledge/kimwog/instance \
  record --source kimwog --id <item-id>
```

### 2. refract, never recite

- cross every warehouse claim with the user ledger context injected every turn
  (active goals / constraints / resources / preferences / supplement stack /
  engagement level). Knowledge that conflicts with a user SAFETY constraint
  LOSES -- constraint wins, say so (e.g. injury constraint beats any
  volume-dose claim).
- personalized numbers (doses, per-kg scaling): NEVER freehand arithmetic.
  RESCALE only via the engine (deterministic; the model speaks the result,
  computes nothing):

```bash
python3 - <<'PYEOF'
import json, sys
sys.path.insert(0, "knowledge/kimwog/tools")
import kimwog, refract
fm, _ = kimwog.read_frontmatter("knowledge/kimwog/items/<item-file>.md")
ledger = refract.Ledger.from_root("knowledge/kimwog")
adapter = refract.DictLifekitAdapter({
    "body_weight_kg": {"value": <measured-kg>, "measured_at": "<YYYY-MM-DD>"}})
profile = {}  # stated axes from ledger injection, if any
res = refract.refract(fm, ledger, adapter, profile, now="<today YYYY-MM-DD>")
print(json.dumps(res.to_dict(), indent=2, ensure_ascii=True))
PYEOF
```

  measured values come from lifekit (`$PROJECT_ROOT/database/lifekit.sh
  body-state` / metric history), stated values from the ledger injection.
- result handling: `ops[].result_text` = the ONLY new numbers you may speak.
  `block_specifics: true` -> a safety axis is unknown/refused: withhold
  specific numbers and any green-light, still give general info + mechanism
  (NOT silence). `needed_missing` -> answer FIRST with the baseline_text
  baseline, ask per budget only if `reask_ok`.

### 3. engagement ladder (depth proportional, DGN-229 5 principles)

- 0 background watch (DB anomaly only, no unsolicited depth) / 1 symptom
  mention (1 physiology sentence + 1 action) / 2 casual gear-purchase
  (budget-fit answer only) / 3 training question (mechanism + ranking) /
  4 explicit request-project (full depth). Read current engagement from ledger
  injection; do not jump levels uninvited.
- supplement recommendation (creatine, caffeine, etc.) REQUIRES stack audit
  FIRST: read current supplement stack from ledger injection; stack unknown ->
  ask what they take before recommending anything.
- numbers (doses / kcal / volume targets) speak ONLY via skill path: refract
  RESCALE, lifekit agg, ledger safety caps. Never freehand.
- resources first: check available equipment / time slots / schedule (ledger)
  before proposing a program. Unusual request -> probe intent before
  optimizing it.
- restriction-day performance warning inline (hard cut + heavy training day ->
  say the tradeoff in the same answer).

### 4. absence honesty (warehouse miss)

- no item covers the ask (or render=missing): SAY the warehouse does not cover
  it yet ("지식창고에 아직 없는 주제예요"), then answer from general knowledge
  LABELED as such ("일반 지식 기준으로는...") -- no fake warehouse authority.
  Expected today for programming-craft asks (@gym-craft lane is empty in
  warehouse #1).
- log the gap for warehouse accretion (append ONE line):

```bash
echo "- $(date +%F) exercise: <asked-topic> (warehouse miss)" >> knowledge/kimwog/GAPS-instance.md
```

### 5. heavy design -> delegate

- full program redesign / phase planning (multi-week mesocycle structure) ->
  delegate to a subagent with an EXPLICIT model per AGENT.md Model routing
  (report-only, one-writer: subagent reports, this session writes). Do not
  grind full-depth design inline in a live turn.

---

## knowledge warehouse consumption

when commenting after a logged workout or during training design, consult the warehouse FIRST:

warehouse root: `<workspace>/knowledge/kimwog` (snapshot pinned; check `.snapshot-pin` for release).
relevant domains: exercise, sleep-recovery (via registry.yaml; use resolve.py for aliases).

procedure:
1. read candidate items from `items/` for the relevant domain (exercise, sleep-recovery).
2. check `e/` for owner E-records on the same domain.
3. run refraction: `python3 knowledge/kimwog/tools/refract_cli.py <item-id> --measured body_weight_kg=<kg> --now <YYYY-MM-DD>`
   item-id = canonical slash form from item frontmatter (e.g. `exercise/creatine-ergogenic-001`).
   body_weight_kg: read from `database/lifekit.sh body-state` (weight_kg field). pass --now with today's date.
   refract_cli.py is deterministic -- pass measured values; do not compute numbers yourself.
4. speak the refracted result in post-log comments. never recite raw item text, claim text,
   or grade letter in user-facing output (M-2 guard). apply knowledge to owner's actual
   pattern -- refracted, not recited.
5. if no warehouse item applies to today's workout event: no comment from the warehouse; skip silently.

grade/pin awareness: grade A = converged consensus; D = practitioner; E = owner E-record.
contested items: always hedge. PROVISIONAL items (marked in item body): phrase as this
program's working rule, never "science says".
