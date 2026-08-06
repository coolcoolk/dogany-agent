---
name: relationship-care
display_name: 관계 케어
description: >-
  관계 케어 -- 사람 관련 모든 행동: 만남 전 브리핑, 레트로 후 관계 기록,
  연락 텀 알림, 가벼운 안부 체크인, 터치 로깅. 트리거(KO): "OO 만나기 전에 뭐 알아야 해",
  "오늘 만난 사람", "OO랑 통화했어", "OO한테 DM했어", "연락 안 한 사람 있어?",
  "OO 요즘 어때", "관계 케어", "누구 연락해야 해", "지난달에 OO 만났었어".
  트리거(EN): "pre-meeting brief", "who should I contact", "log a touch",
  "relationship retro". Outputs: brief (0 questions), retro questions
  (max 2/day total), contact-gap suggestions + at most one casual check-in
  (all inside 1 batch/week via alert-pick only), touch log (1 turn).
---

# relationship-care

Model routing: sonnet (data-wrangling, brief assembly). haiku for one-liner touch logs.

Cognitive budget (declare to user if asked):
- retro questions: <=2 per day total (not per person)
- proactive gap suggestions: <=1 batch/week (2-3 persons max), gated by alert-pick
- check-in question: rides INSIDE the same weekly 1-batch cap (alert-pick emits it
  only with spare slots) -- no separate budget line
- touch log: 1 turn, done
- brief: 0 questions, pure output

Resolve persons: always run lifekit.sh person-find first; get person id.
relmod takes ids, not names.

Tools:
- $PROJECT_ROOT/database/lifekit.sh -- person-find, appointments
- $PROJECT_ROOT/database/relmod.sh -- all relationship verbs

## behavior 1: pre-meeting brief

Triggers: user mentions meeting someone / asks about a person / upcoming appointment.

Steps:
1. person-find -> get id
2. relmod.sh brief <id>
3. output result, no questions

Format:
- profile line (relation, level, birthday if within 30d warning)
- last contact + kind
- recent facts (up to 5)
- upcoming meetings from lifekit

Zero questions. Pure output.

## behavior 2: retro enrichment

Triggers: daily retro happens / user casually reviews their day.

Steps:
1. relmod.sh retro-candidates [--date YYYY-MM-DD]
2. if NONE -> skip silently
3. pick person with fewest facts (lowest fact-count column) or oldest fact date
4. ask AT MOST 1-2 light questions TOTAL for the day (not per person)
   example: "오늘 만난 OO님 근황 하나만 -- 요즘 뭐 하신대요?"
5. user answers -> relmod.sh fact-add <id> "<fact>" --source retro
6. user brushes off / no answer -> drop silently, never nag

Budget: 2 questions maximum across ALL persons that day. Stop after 2 even if more candidates.

## behavior 3: contact-gap suggestions + casual check-in

Trigger: proactive agent turn (weekly surface) or user asks "누구 연락해야 해?".

PICK line format (tab-separated): PICK <pid> <name> <gap_days>days <ratio> <hint> <meet_days> <meet_ctx>
- meet_days = days since last MEETING (만남); '' if no meeting on record
- meet_ctx = occasion/title of last MEETING; '' if no meeting on record
- basis is last MEETING (만남), NOT last 대화

Steps:
1. relmod.sh alert-pick
2. if output = CAP_REACHED -> say nothing proactively (silence)
3. if output = NONE -> say nothing
4. if PICK lines -> render compact list, exactly this format:
   💬 연락해보세요
   이름 | 맥락 | n일전
   where 맥락 = meet_ctx (use "-" if empty), n일전 = meet_days + "일전" (use "-" if empty)
   example:
     💬 연락해보세요
     최정인 | 감자반 | 12일전
     문채림 | 채림 여명 예담 | 20일전
   NEVER reword rows back into full sentences.
   NEVER add guilt / urgency / streak wording.
   NEVER frame as obligation or task.
5. if ASK_FADE line -> ask once gently: "OO님이랑 연락이 많이 뜸해졌는데, 그냥 자연스럽게 멀어지는 게 낫겠다 싶으면 알려줘요."
   user says yes -> relmod.sh set-fade <id> on
   user says no -> note OK, no further action
6. if CHECKIN line (person we have NO contact record for) -> ask ONE casual
   curiosity-style question, friendly, zero obligation:
   "요즘 OO님 근황 아세요? 최근에 본 적 있어요?"
   NEVER frame as "you should contact them". It is curiosity, not a task.
   user reply "아 이때/지난달에 만났었어" -> log the recalled meeting:
   - exact date + occasion known -> prefer registering the past appointment
     natively via the appointment-log skill (lifekit)
   - fuzzy/approximate date -> relmod.sh touch-add <id> meet --date <approx-date>
     one turn, done
   user shrugs / no info -> drop silently
7. user contacts/meets someone after suggestion -> relmod.sh acted <id>
8. user dismisses suggestion -> relmod.sh dismiss <id>

Rules:
- NEVER hand-roll gap logic from raw queries; always go through alert-pick
- CAP_REACHED means the cap fired this week; say nothing proactively
- 맥락 and n일전 columns are MEETING-based (from meet_ctx / meet_days fields); NOT 대화-based
- alert-pick already excludes anyone with an upcoming appointment in lifekit; do NOT re-implement that filter here
- CHECKIN is capped in code (max 1, 60-day per-person window, only with spare
  pick slots) -- never ask more than the one question alert-pick surfaced
- alert-pick also excludes anyone with an active snooze (snooze_until > today);
  see behavior 3b for snooze mechanics
- RESURFACE\t<pid>\t<name> line from alert-pick means a snooze just expired;
  ask ONE re-confirm question before resuming normal suggestions (see behavior 3b)

## behavior 3b: context snooze (temporary away)

Trigger: user states a person is temporarily unreachable / away.
Examples: "쟤 미국 갔어", "유학 갔어", "당분간 못 봐".

This is NOT fade. Fade = permanent drift-apart (behavior 5 / set-fade, unchanged).
Snooze = temporary, dated pause with a reason. Do not use fade for "temporarily away".

### Confirm gate

1. Detect context implying temporary absence.
2. If user did NOT use an explicit removal verb ("그냥 빼줘" / "빼둬" / "빼둬라"):
   ask ONE confirm: "그럼 연락 제안에서 당분간 빼둘까요?"
   - Yes -> proceed to snooze.
   - No / silence -> drop, no action.
3. If user explicitly said a removal verb -> skip confirm, snooze directly.
NEVER auto-snooze from context alone without confirm (too error-prone).

### Duration

- Explicit timeframe given ("2년", "내년 여름", concrete date):
  -> parse to --months N or --until YYYY-MM-DD and pass to relmod.
- No timeframe given -> call relmod.sh with no duration flag (relmod defaults to 6 months).

### Snooze call

Steps:
1. person-find -> id
2. relmod.sh snooze <id> [--until YYYY-MM-DD | --months N] [--reason "<reason>"]
   Pass --reason with the stated reason so it is recorded as a fact on that person.
3. One confirmation line, done.

Effect: person excluded from alert-pick PICK lines AND from CHECKIN until snooze_until.

### Manual clear (early return)

User says person is back early ("쟤 돌아왔어" / "이제 봐도 돼"):
1. person-find -> id
2. relmod.sh unsnooze <id>
3. One confirmation line. Normal suggestions resume automatically.

### RESURFACE re-confirm (snooze expiry)

When alert-pick emits `RESURFACE\t<pid>\t<name>` (fires exactly once, snooze already cleared):
1. Ask ONE question: "OO님 아직 그대로예요?"
2. Still away -> snooze again:
   - ask / derive new duration; if none given, default 6 months.
   - run relmod.sh snooze <id> [...] as above.
3. Back / normal -> do nothing. Snooze already cleared; suggestions resume automatically.
Never fire RESURFACE handling more than once per expiry event.

## behavior 4: touch logging

Triggers: "OO랑 통화했어", "OO한테 DM했어", "OO한테 메시지 남겼어", "met X",
"지난달에 OO 만났었어" (recalled meeting, fuzzy date OK).

Steps:
1. person-find -> id
2. relmod.sh touch-add <id> <kind> [--date YYYY-MM-DD] [--note "..."]
   kind: call | message | dm | other | meet
   meet = a real meeting the user recalls that was never logged; approximate
   date allowed. If exact date + occasion known, prefer appointment-log skill.
3. one confirmation line, done

1 turn total. No follow-up questions.

## behavior 5: level change

User mentions someone feels closer / more distant.

Steps:
1. check relmod.sh drift-list for existing UPGRADE suggestion for that person
2. SUGGEST the change, never auto-apply
3. user confirms -> note it (fact-add with source=chat), do NOT update intimacy_id in lifekit
   (level changes are a separate lifekit.sh operation; ask user to confirm via lifekit.sh)

Downgrades: only suggest if user explicitly signals distance + repeated dismisses.
Never suggest downgrade from numbers alone.

## behavior 6: remote friends (contact mode)

User says someone lives far away / only DMs:
-> relmod.sh set-mode <id> contact

Their touches (DMs, calls, recalled meetings) are the tracked channel; meetings not expected.

## behavior 7: onboarding (first activation)

One turn only:
- ask for family / partner / pets only (not full social graph)
- register via lifekit.sh person-add <name> <relation>
- store base facts via relmod.sh fact-add <id> "<fact>" --source onboarding
- never re-ask what already exists in lifekit persons table
