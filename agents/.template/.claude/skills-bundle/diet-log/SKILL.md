---
name: diet-log
display_name: 식단 기록
description: >
  Handles BOTH diet logging AND diet/calorie status queries. Both cases go through
  this skill.
  (1) Log: when __USER_LABEL__ says they ate something or asks to record a meal.
  "I had breakfast", "I had samgyeopsal for lunch", "ate nuts as a snack", any
  food name + amount, sending a barcode image or number, sending a nutrition-label
  photo - any meal-input situation. Look up the food nutrition DB first, and if
  not found, estimate with the LLM and record into the local lifekit.db meals table.
  (2) Status query: "today's calorie status", "diet status", "what did I eat today",
  "how much have I eaten", "tell me today's/this week's calorie goal", "how many
  calories do I have left", "how much have I taken in" - any utterance asking that
  day's intake/goal/remaining. Do NOT hand-write raw SQL here either; answer with
  this skill's lifekit.sh agg-day + card.py. Ownership: diet-log owns intake and
  remaining-calorie queries; for burn/balance queries defer to workout-log.
  Whether logging or querying, the final output is always a status card (render to
  files/outbox/ + send via send_file::).
  When a log succeeds, a PostToolUse hook (card-followup) injects a hard instruction
  that "the next action MUST be render + send the card". When that instruction
  arrives, build and send the card before any other reply (do not defer it to a
  later turn).
---

# diet-log

## overview

__USER_LABEL__ gives food/diet info ->
1. local cache -> public food nutrition DB lookup (exact values preferred)
2. DB miss -> (brand: web search first) -> LLM estimate
3. record in local lifekit.db meals table via `lifekit.sh meal-add`

All DB access via `lifekit.sh` (= lifekit.py core). No raw SQL.

## paths

- helper: `$PROJECT_ROOT/database/lifekit.sh` (PROJECT_ROOT unset -> `LKIT="${PROJECT_ROOT:-$(pwd)}/database/lifekit.sh"`; CWD must be workspace root)
- skill files: `$PROJECT_ROOT/.claude/skills/diet-log/` (lookup.py, card.py, fonts/)
- relative paths below assume cwd = PROJECT_ROOT. do not cd into skill dir.

## API keys / endpoints (optional)

Keys not stored plaintext. `lookup.py` reads from project `.env`
(priority: `DIET_ENV_FILE` env -> `$PROJECT_ROOT/.env` -> `.telegram_bot/.env` -> `runtime/.env`)
or process environment.

- `FOODSAFETY_KEY` — 식품안전나라 C005 (barcode lookup)
- `DATAGOV_KEY` — 공공데이터포털 nutrition DB
- key absent -> public DB lookup returns `{"found": false}` silently; local cache + LLM estimate continue (no crash).

## record target: lifekit.db (local SQLite, SoT)

- meals table. helper = `$PROJECT_ROOT/database/lifekit.sh`.
- area_id: lifekit.sh auto-links to "식습관" area (no manual spec needed).
- kcal = DB generated: protein×4 + fat×9 + net_carb(carb−fiber)×4 + alcohol×7. pass macros only (no kcal input).
- columns: date, meal(아침/점심/저녁/간식/운동), name, grams, carb, protein, fat, fiber, sugar, alt_sugar, alcohol.
- `alcohol` = pure alcohol grams (optional, 7kcal/g). required for alcohol items to get accurate kcal.

## procedure

### A. input by food name

```bash
python3 .claude/skills/diet-log/lookup.py name "food_name" <grams>
```

lookup.py checks local cache first. cache hit -> returns `source:"cache"` immediately (0 tokens).

lookup order: cache -> public DB (auto). both miss -> branch on brand:

- brand present (franchise/maker name on food) -> web search required. find official/trusted nutrition source. do not skip to estimate. if 2+ sources agree -> trusted value. on find -> `cache-add` (`estimated:false`) for next lookup at 0 tokens. web also fails -> LLM estimate (`estimated:true`).
- no brand (generic: 백미밥, 삼겹살, 사과) -> skip web, go straight to LLM estimate. if reliable -> cache with `estimated:true`.

priority: 1) local cache -> 2) public DB -> 3) web/brand official (required for branded, `cache-add estimated:false`) -> 4) LLM estimate.

### B. input by barcode

```bash
python3 .claude/skills/diet-log/lookup.py barcode "barcode_number" <grams>
```

### C. input by nutrition label photo

Read values directly from image -> record via meal-add. also cache-add the label values.
Label = __USER_LABEL__-provided official values -> highest trust -> `estimated:false`.
Same product next time -> 0-token lookup. `key_name` = product name, `grams` = serving size on label.

### D. cache-add

```bash
python3 .claude/skills/diet-log/lookup.py cache-add '<json>'
```

JSON structure (values = absolute amount for 1 serving at `grams`):

```json
{
  "key_name": "제품명 (표시옵션)",
  "name": "표시용 원래 이름",
  "grams": 347,
  "kcal": 553,
  "carbs": 40, "protein": 18.7, "fat": 18.7,
  "fiber": 5, "sugar": 5, "sodium_mg": 430,
  "source": "출처",
  "estimated": true,
  "updated": "2026-06-25"
}
```

- `key_name`: raw name before normalization. lookup.py lowercases, strips brackets/special chars, trims spaces (same function used for lookup).
- `grams`: serving size the values are based on (g). lookup scales proportionally to requested grams.
- `updated`: caller fills today's date (script does not auto-generate).
- same normalized key -> upsert (overwrite).
- cache file: `$PROJECT_ROOT/database/food_cache.json` (personal data, do not commit to repo).

### common flow

1. run lookup.py -> parse JSON result
2. `found: true` -> use DB values. `found: false` -> (brand: web search) -> LLM estimate
3. multiple candidates -> pick closest by food name + maker (ambiguous -> ask __USER_LABEL__)
4. grams unknown -> ask __USER_LABEL__ before recording
5. meal type (아침/점심/저녁/간식) unknown -> ask then record
6. record via lifekit.sh meal-add

```bash
$PROJECT_ROOT/database/lifekit.sh meal-add \
  "<YYYY-MM-DD>" "<아침|점심|저녁|간식|운동>" "<food_name (Xg)>" \
  <carb> <protein> <fat> <fiber> <sugar> <alt_sugar> <grams> [alcohol]
```

- arg order: date meal name carb protein fat fiber sugar alt_sugar grams [alcohol]
- macros (g) only — never pass kcal (DB auto-calculates). unknown macro = 0. grams unknown -> omit last arg (NULL).
- `alcohol` (optional, last): pure alcohol grams. omit = 0. alcohol items: must fill (7kcal/g, not captured in macros -> undercounts if missing).
  - pure alcohol g = volume(ml) × ABV(decimal) × 0.789(ethanol density).
  - carb = actual carb grams as-is. do not fold alcohol into carb (double-count).
- output: `id<TAB>name<TAB>kcal` (kcal = DB auto-calculated).
- convention: include amount in name with parentheses: "데일리견과 핑크 (20g)".
- multi-item meal (composite, same date + meal slot): the reconcile guard blocks duplicate (date, meal) inserts after the first row (exits with code 3). for the 1st item use the standard command above (upsert). for every subsequent item in the same slot, append `--new` to force insert and bypass the guard:
  ```bash
  $PROJECT_ROOT/database/lifekit.sh meal-add \
    "<YYYY-MM-DD>" "<끼니>" "<food_name (Xg)>" \
    <carb> <protein> <fat> <fiber> <sugar> <alt_sugar> <grams> [alcohol] --new
  ```
- user-facing message when splitting a multi-item meal: use the user's language only (e.g. "여러 항목을 나눠서 기록할게요"). never expose internal flags, match keys, or English mechanics to the user (DGN-210 i18n baseline + RULES output rules).
- NEVER narrate scaling/composite step mechanics to the user. do NOT emit planning/step lines such as "Scaled values confirmed", "Now record both as a composite entry", or ANY English working text. portion scaling and per-row `--new` inserts are silent internal mechanics; the ONLY user-facing output is the final record summary in the user's language. English skill text here is internal working material, never a speaking register (RULES output discipline; DGN-503).

### update existing record (meal-upd)

edit in place, do not delete + re-add.

```bash
$PROJECT_ROOT/database/lifekit.sh meal-upd <id> field=value [field=value ...]
```

- fields: `date meal name carb protein fat fiber sugar alt_sugar grams alcohol` (only specified fields update).
- `kcal` is generated — edit macros, kcal auto-recalculates.
- find id via `meal-find <date>` (TSV) or `meal-day <date>`.

## AMT_NUM field mapping (per 100g)

| field | nutrient | unit |
|------|---------|------|
| AMT_NUM1 | 에너지 | kcal |
| AMT_NUM3 | 단백질 | g |
| AMT_NUM4 | 지방 | g |
| AMT_NUM6 | 탄수화물 | g |
| AMT_NUM7 | 식이섬유 | g |
| AMT_NUM8 | 당류 | g |
| AMT_NUM13 | 나트륨 | mg |
| AMT_NUM24 | 포화지방 | g |
| AMT_NUM25 | 트랜스지방 | g |

## report format (to __USER_LABEL__)

after recording, brief report:
```
데일리견과 핑크 20g — 간식 기록 완료
에너지 122kcal / 단백질 3.6g / 지방 9.2g / 탄수화물 6.3g
(DB 조회값)
```

DB miss -> mark `(LLM 추정값)`.

---

## diet status card

two triggers for card generation:
1. auto (forced) — after every successful record. `meal-add` success -> PostToolUse hook (routines/card-followup.py)
   injects additionalContext: "next action = card render + send_file:: now, before any other reply, do not defer."
   on receiving this instruction -> render and send card this turn. hook fires even on raw Bash meal-add (no skill path needed) = card skip structurally blocked.
2. explicit request — "오늘 식단 현황", "칼로리 현황", "식단 얼마나 먹었어", "오늘 뭐 먹었지",
   "목표 칼로리 알려줘" etc. -> handle with this skill, answer via card.

### card script

```
.claude/skills/diet-log/card.py
```

### usage

```bash
python3 .claude/skills/diet-log/card.py '<json>'
```

### JSON input structure

data source = lifekit.db -> usually pass empty or date only. card reads meals/exercise for the date (default today KST) directly from lifekit.db if `meals`/`intake_kcal` absent. all keys optional; if passed, overrides lifekit.db auto-aggregate.

```json
{
  "date": "2026-06-25",
  "output": "files/outbox/diet_card.png"
}
```

- `date`: "YYYY-MM-DD" for lifekit.db lookup (omit = today KST). card header auto-converts to "YYYY.MM.DD 요일".
- `meals[].protein/carbs/fat`: optional. if present, shows "단 N  탄 N  지 N g" right of meal row.
- `meals[].sugar` / `carbs.sugar`: optional. sugar (g). lifekit.db auto-aggregate fills from meals.sugar.
- `burn_kcal`: omit -> hide burn label and exercise segment. if exercise present -> intake bar splits into [base+activity−deficit] + [exercise bonus], showing how exercise expands the target.
- `output`: omit -> `/tmp/diet_card.png`.

### aggregate today's diet/exercise from lifekit.db

```bash
python3 .claude/skills/diet-log/card.py '{}'                    # today (auto)
python3 .claude/skills/diet-log/card.py '{"date":"2026-06-25"}'  # specific date
```

manual check:

```bash
$PROJECT_ROOT/database/lifekit.sh agg-day "$(date +%F)"       # intake/macros/burn/balance
$PROJECT_ROOT/database/lifekit.sh meal-day "$(date +%F)"      # meal list
$PROJECT_ROOT/database/lifekit.sh workout-find "$(date +%F)"  # workout list
```

after sending card -> text must not repeat card numbers. text = only comments/nudges not already on the card. no meta-phrases like "코멘트만 짧게:" — go straight to substance.

### card renderer: python/font resolution (path-independent)

card.py needs matplotlib. resolve interpreter:

```bash
RENDER_PY="${RENDER_PYTHON:-}"
[ -z "$RENDER_PY" ] && [ -x "$HOME/dogany/.venvs/render/bin/python" ] && RENDER_PY="$HOME/dogany/.venvs/render/bin/python"
[ -z "$RENDER_PY" ] && command -v python3 >/dev/null && python3 -c 'import matplotlib' 2>/dev/null && RENDER_PY="$(command -v python3)"
[ -z "$RENDER_PY" ] && RENDER_PY="python3"   # last fallback (card.py exits code 3 gracefully if missing)

OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{"output":"files/outbox/diet_card.png"}')
```

- python resolution priority: `RENDER_PYTHON` (env) -> `~/dogany/.venvs/render` -> PATH python3 (only if matplotlib present) -> `python3`.
- card.py: matplotlib not found -> exit code 3 + stderr message (graceful skip). report text-only: "카드 렌더 스킵 — matplotlib 미설치".
- fonts: bundled `fonts/ASDGN_*.ttf` preferred (path-independent). absent -> try system TTC (`DIET_CARD_TTC` env or default candidates). also absent -> matplotlib default font (CJK may break, warning only).

### card delivery

delivery path branch: card.py default output = `/tmp/diet_card.png`. /tmp is outside PROJECT_ROOT -> `send_file::` marker in live turns cannot auto-send it (see RULES Files). live turn -> output to `files/outbox/`, send via `send_file::`. cron/bot direct-send -> push.sh can send /tmp too.

```bash
# live turn (direct reply to __USER_LABEL__): output to outbox then send_file:: <absolute_path>
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{"output":"files/outbox/diet_card.png"}')

# cron/routine (bot direct-send):
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{}')
routines/push.sh --photo "$OUT"
```

### target values (card.py auto-calculates from body stats — no input needed)

target kcal = auto-calculated from lifekit.db config table body stats. do not ask __USER_LABEL__ for total kcal.

read before asking: do not re-ask weight/goal_mode/protein/fat/carb targets.
first run `$PROJECT_ROOT/database/lifekit.sh body-state` to read current goal_mode, weight, eff_goal, macro targets.
lifekit values are canonical. only ask __USER_LABEL__ if truly absent after lookup.

single source of truth: lifekit config table (SqliteConfigStore).
fields: weight_kg, height_cm, skeletal_muscle_kg, fat_mass_kg, lean_mass_kg, avg_steps,
deficit_kcal, other_neat_kcal, protein_g, fat_ratio, goal_mode, updated.

| item | formula |
|---|---|
| BMR | 370 + 21.6 × lean_mass_kg  (Katch-McArdle) |
| NEAT (activity burn) | walk(0.5 × weight × dist_km, dist=steps×stride) + other_neat_kcal |
| intake target (eff_goal) | BMR + NEAT − deficit_kcal + that_day_exercise_burn |
| protein | protein_g fixed |
| fat | eff_goal × fat_ratio ÷ 9 |
| carb | (eff_goal − protein×4 − fat_kcal) ÷ 4 |

- lean_mass_kg absent -> weight_kg − fat_mass_kg. stride = height × 0.415.
- exercise burn = workout DB record (`소모 칼로리`) passed as burn_kcal.current. NEAT (walk/daily) = auto from stats.
- do not update config table via raw SQL — use CLI: `$PROJECT_ROOT/database/lifekit.sh config-set weight_kg=72 goal_mode=recomp ...` ('updated' auto-set).
- also log measurement time-series: `$PROJECT_ROOT/database/lifekit.sh log-metric <YYYY-MM-DD> weight_kg 72`.
