---
name: spending-log
display_name: 소비 기록
description: >
  Handles ALL spend recording (C1) AND spend queries AND weekly ledger sessions (C2).
  All cases go through this skill.
  (1) Log: any spend utterance -- "커피 5500", "점심 김치찌개 9천원 카드",
  "월세 65 나갔어", "어제 택시 12,000원", "쿠팡에서 3만원 썼어",
  "무이자 할부로 샀어", "환불 받았어", "취소했어" -- any item + amount,
  any payment method mention, refund/cancel, installment.
  Parse immediately and call spend-add in the same turn. Never block recording.
  Uncertain category -> 기타. Business-suspect -> scope=unknown silently.
  (2) Status query: "오늘 얼마 썼어", "이번 주 지출 얼마야", "이번 달 가계부 보여줘",
  "지출 현황", "고정비 얼마 나가", "이번 달 카드값" -- any utterance asking
  day/week/month totals, category breakdown, fixed costs, or card spend.
  Answer via spend-day/spend-week/spend-month (never raw SQL).
  (3) Weekly ledger session: routine instance '가계부 확인' appearing on task surface,
  or "가계부 정리하자", "가계부 확인하자" -- convene full C2 session: review,
  batch-fill, unknown sweep, reclassify.
  All records/queries go through lifekit.sh spend-* verbs (never raw SQL).
  Final report = Telegram text; simple tables as fenced code block.
  (4) Payline management (v1.1): card/account registration and fixed-cost management -- "온보딩하자", "소비 온보딩", "카드 등록하자", "고정비 등록해줘", "월세 고정비로 넣어줘", "이번 달 카드 실적 얼마나 채웠어", "실적 현황", "다음 주 뭐 빠져나가", "고정비 언제 나가". Registration via method-*/fixed-* verbs; perf answers always carry the ledger-approximation hedge.
  Capture-routing (DGN-566): spending is an ACTIVE module -- any spend mention routes here, NEVER into generic memory; confirmation speaks in ledger terms (가계부에 기록했어요), never 기억해둘게요.
---

# spending-log

## overview

C1 (capture): user mentions a spend -> parse -> spend-add immediately, 1 turn.
Do not block recording: uncertain category -> 기타 + note; business-suspect ->
scope=unknown silently. C2 sweep fixes classification later.

C2 (weekly session): routine '가계부 확인' (Sunday 10:00, lifekit routine_def,
roller-generated task instance) convenes it. Session = review + reclassify, not
manual bookkeeping.

All DB access via `$PROJECT_ROOT/database/lifekit.sh`. No raw SQL ever.

## paths

- helper: `$PROJECT_ROOT/database/lifekit.sh` (cwd must be workspace root;
  same convention as diet-log: `LKIT="${PROJECT_ROOT:-$(pwd)}/database/lifekit.sh"`)

## record target

- tables: spend_entry + spend_category (lifekit.db, migration 009, user_version 9).
- amount = KRW integer only (no floats). expense > 0, refund/cancel < 0.
- scope: `personal` (default) | `business` | `unknown`. business rows recorded
  but EXCLUDED from personal reports. aggregates default personal-only; `--all`
  includes everything.
- source: value set `chat` (C1 default) | `batch` (C2 batch fill) | `auto`
  (v1.1 auto-entry rows, migration 010+). verb layer enforces;
  `import`/`receipt` remain v2, invalid now.
- is_fixed: omit -> inherits category `is_fixed_default`. Only 주거/인프라 has
  is_fixed_default=1. All other fixedness = per-row flag via fixed-vocab rule
  (see C1 parsing).
- category dictionary: seed v2 (domain-aligned, 2026-07-25 owner decision) --
  주거/인프라, 식생활, 카페/간식, 건강, 이동, 콘텐츠/취미, 학습, 관계, 모밀이,
  여행, 생활용품, 기타. Category = life-domain lens (matches estate domain map:
  건강<->workout/health data, 여행<->travel blocks, 콘텐츠/취미<->music/content,
  관계<->relationship records). NO subscription category by design: subscription
  = payment shape, not a "what" -- each subscription goes to its domain category
  with is_fixed=1 (넷플릭스 -> 콘텐츠/취미 fixed, 헬스장 -> 건강 fixed, 월세 ->
  주거/인프라 fixed). Unknown category name -> auto-registered
  (is_fixed_default=0), tool never fails. NULL category renders as '(미분류)' --
  distinct from real '기타'.

## C1 parsing rules

- amount notation: "5500" / "5,500원" / "9천원"=9000 / "6.5만"=65000 /
  "65만"=650000; rent-class context ("월세 65") = 만원 units = 650000.
  Amount is the ONE field never guessed: if genuinely ambiguous, ask once.
  Everything else never blocks recording.
- date: omitted -> today KST. "어제" -> yesterday. Format YYYY-MM-DD.
- category: infer from item name against seed v2. Uncertain -> 기타 + original
  phrase in note; reclassify at C2. Create NEW category only when user
  explicitly names one.
- fixed-vocab rule: utterance contains subscription/recurring-shape vocab
  (구독, 멤버십, 회원권, 보험, 월세, 공과금, 통신요금, 정기결제, 연간권) ->
  pass `--fixed 1` regardless of category. One-off spends in those same
  categories stay variable (default).
- method: record if stated (카드/현금/체크, card name, 계좌); else omit (NULL).
- scope: explicit 사업/업무/거래처 -> business. Business-SUSPECT (could be
  either) -> scope=unknown, do NOT ask, C2 sweep resolves. Clearly personal
  -> personal.
- name convention: item description in user language.

## verb surface (verified signatures)

```
lifekit.sh spend-add <date> <amount> <name> [category] [method] [scope] [note] [--fixed 0|1] [--new] [--source chat|batch]
    output: id<TAB>name<TAB>amount<TAB>category

lifekit.sh spend-find <date> [--all]
    output TSV: id name amount category method scope

lifekit.sh spend-day <date> [--all]
    human-readable day list + total

lifekit.sh spend-del <id>

lifekit.sh spend-upd <id> field=value [...]
    fields: date name amount method note is_fixed scope source category
    (partial update; amount/is_fixed strict int, bad value = clean rc=1 error)

lifekit.sh spend-week <monday-date> [--all]
    total/count, prev week, diff, per-category

lifekit.sh spend-month <YYYY-MM> [--all]
    total, fixed vs variable, category top5 + 그외 remainder line,
    per-method subtotal, prev month diff

lifekit.sh spend-unknown
    ALL scope=unknown rows across all dates + count/total footer (C2 sweep surface)
```

## v1.1 payline surface (migration 010)

verbs (locked spec 3.2):
```
lifekit.sh method-add <name> [kind] [aliases] [billing_day] [perf_base] [perf_window] [perf_note] [benefit_note]
lifekit.sh method-list [--all] / method-upd <id> field=value / method-retire <id>
lifekit.sh fixed-add <name> <amount|-> <category> <next_due> [pay_day] [pay_mode] [method] [cycle] [note]
lifekit.sh fixed-list [--all] / fixed-upd <id> field=value / fixed-retire <id>
lifekit.sh fixed-due <YYYY-MM-DD|YYYY-MM>
lifekit.sh fixed-paid <id> [date] [amount]
lifekit.sh perf-status <YYYY-MM>
```

rules:
- alias normalization at WRITE time: C1 parse matches method utterance against
  pay_method name+aliases -> write the CANONICAL name string into the record.
  No match -> keep free text as-is. Query-time alias matching = legacy-row
  fallback only.
- fixed-add is personal-only (v1.1): business fixed-cost attempt -> refuse,
  user-language message meaning "v1.1 고정비는 개인 전용 -- 사업 고정비는 비즈킷에서".
  Never register with a workaround.
- billing_day = info/notice only. Card BILL payment is NEVER recorded as a
  spend (the underlying purchases are the spend; recording the bill too =
  double count).
- amount '-' (variable-amount fixed cost) = never auto-entered; notice only,
  record via fixed-paid after amount confirmed.
- fixed-paid = manual fulfillment: writes one linked ledger row (is_fixed=1) +
  rolls next_due. Use it, never a bare spend-add, for registered fixed costs.
- perf-status: ledger-based APPROXIMATION. Sum = ALL scopes (issuers count
  business spend too). EVERY perf output/notice carries the fixed hedge
  "장부 합산 기준(근사)" + the card's perf_note. Installment recognition mismatch
  is part of the approximation.
- retire semantics: method-retire refused while active fixed costs reference it
  -> present the list, reassign via fixed-upd method_id first.

- duplicate gate (DGN-231 reconcile-before-write, tool-enforced): spend-add
  match key = (date, amount) exact. On match: prints "EXISTS n" + matching
  rows, exit code 3, nothing written. Handling: show matched row to user in
  user language, confirm it is genuinely a second spend (e.g. two coffees same
  price), then retry with `--new`. NEVER pre-emptively pass `--new` on first
  attempt. NEVER silently drop the record.
- aggregates (week/month/find/day) default scope=personal. `--all` = include
  business/unknown.

## record conventions

- refund/cancel: ONE negative row, dated the refund day. Never mutate the
  original purchase row (append-first ledger).
- foreign currency: amount = confirmed KRW billed amount; original currency in
  note ("USD 12.99"). Before settlement: record expected KRW, then spend-upd
  amount on confirmation.
- installment: full amount, one row, purchase date + note '할부 N개월'. No
  monthly split rows.

## C2 weekly session procedure

1. Run spend-week (this week's Monday) -> present: "이번 주 이렇게 잡혔고, 빠진 거 있어요?" style.
2. Batch-fill missing entries: spend-add with `--source batch`. Duplicate gate
   catches C1-prerecorded rows (EXISTS + exit 3) -> skip or confirm `--new`.
3. auto-entry confirm (v1.1): list this week's auto-entered rows -> user
   confirms real debit + amounts. Mismatch -> fix amount on the spot; transfer
   failed -> delete the row. Confirmation LIST, not questions (D6).
4. Unknown sweep: spend-unknown -> present all rows -> user classifies on the
   spot -> spend-upd <id> scope=personal|business. Session ends with unknown
   remainder 0 (normal state).
5. Reclassify: propose re-binning of 기타 and (미분류) rows ->
   spend-upd <id> category=<name>.
6. Confirm weekly summary. First session of a month: also run spend-month for
   previous month and join the monthly summary. fixed-cost reconcile:
   fixed-due expected vs actually recorded this month -> surface missed items.
   perf-status shown with hedge.
- End-of-session manual slot: 투자노트 (investment note) -- outside this
  module's scope; keep the slot only, do not absorb. User writes/dictates
  freely.

## session trigger + nudge (mechanics documented, not owned here)

- Convening = lifekit routine_def '가계부 확인' (weekly, Sunday 10:00),
  roller-generated task instance on Ag task surface. Deterministic carrier;
  this skill only documents it.
- Missed-session fallback: 10+ days since last completed C2 session -> mention
  once in next natural conversation; no response -> 1 push. Max once per missed
  session, NO repeats (attention-welfare cap, D5). Judge "last session" by REAL
  state only: latest spend_entry with source='batch' date, or routine instance
  done -- never estimate.

## notice wording (skill owns wording; routine owns timing/caps/sent-state)

- auto item, due-1day: "내일 <이름> N원 빠져나가요" style, one line.
- manual item, due day: "오늘 <이름> 납부일이에요" + if unfulfilled, ONE
  follow-up, then silence.
- yearly item: D-7 advance notice (bigger amount, lead time).
- perf shortfall: window-end D-3, shortfall cards only, hedge + perf_note
  included.
- all notices user-language, module terms, no internal verbs/flags exposed.

## report format

- C1 confirm: one short line. No card, no extra numbers.
  ```
  커피 5,500원 — 카페/간식 기록 완료 (오늘)
  ```
- weekly: 총지출 / 카테고리별 / 전주 대비 증감 / 건수. Telegram text; simple
  table = fenced code block.
- monthly: 총지출 / 고정 vs 변동 / 카테고리 top5 + 그외 / 결제수단별 소계 /
  전월 대비.
- commentary after numbers = observations on recorded data only. NO
  prescriptive finance advice from model memory: any finance-knowledge claim in
  coaching text must come from a verified knowledge source (current verified
  base = 3 confirmed findings, evidence grade B, investment-domain -- finance
  research dossier 2026-07-25); unverified claims never stated as fact. v1
  coaching stays thin: numbers + data observations.

## discipline

- cognitive budget (declared): C1 log = 1 turn, 0 questions by default
  (amount ambiguity = the only permitted question). Category/scope questions
  forbidden at capture time -- deferred to C2 sweep. C2 = the single weekly
  batch touchpoint.
- absence claims: never say "지출 기록 없음" without running spend-day/spend-find
  first (search-before-absence-claim).
- trigger tiers: C1 capture = BEST-EFFORT (this description + Ag agent.md
  habit line). Duplicate gate + value-set enforcement = GUARANTEED (tool
  layer). C2 convening = GUARANTEED (routine_def roller, not this skill).
- user-facing output = user language ONLY. English skill text is internal
  working material, never a speaking register. Never expose verbs/flags/exit
  codes/table names to the user (same i18n baseline as diet-log).
- no outbound network in this skill (local sqlite only).

## onboarding (module onboarding, engagement-adaptive)

- trigger: "온보딩하자", "소비 관리 시작하자", "가계부 세팅하자", "카드 등록하자",
  "고정비 등록해줘" -> start conversational onboarding; depth adapts to user
  engagement, conversation NOT survey.
- tier minimal: no registration, just start recording (0 questions). Offer
  nothing more if user disengages.
- tier standard: card list (name/kind/aliases/billing_day) + fixed-cost list
  (name/amount/category/next_due/pay_mode/method/cycle). Budget (D6, declared):
  max 8 questions, max 4 round-trips total.
- tier deep: + perf conditions (perf_base, perf_window, perf_note,
  benefit_note). Budget: +4 questions, +2 round-trips max.
- overflow rule: budget exceeded -> stop asking, use defaults/blank, tell user
  the rest can be added later anytime (module terms). next_due asked directly
  as "첫/다음 결제일이 언제예요" (no proration math).
- re-runnable anytime; partial registration is normal state.
