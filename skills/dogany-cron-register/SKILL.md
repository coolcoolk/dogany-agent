---
name: dogany-cron-register
description: __USER_LABEL__이 반복작업(크론/정기루틴)을 맡기면 에이전트가 launchd로 끝까지 직접 등록한다. "매일 몇시에 X 해줘", "이거 정기적으로", "크론 걸어줘" 류 요청에 발동. plist 작성→검증→테스트 발송→launchctl load까지 에이전트가 처리하고, 코드만 던지고 __USER_LABEL__께 미루지 않는다. 게이트웨이/메인 봇 재시작만 예외(__USER_LABEL__께 요청).
---

# dogany-cron-register — recurring task launchd registration

__USER_LABEL__ assigns recurring task -> agent handles plist write through launchctl load. (dogany-skill-creator convention output.)

## trigger signals
- "매일/매주 몇시에 X 해줘", "정기적으로", "크론 걸어줘", "루틴으로 만들어"

## core rules
- do not just hand over code to __USER_LABEL__ — agent registers end-to-end.
- exception: gateway/main bot restart/stop -> do not do alone (__USER_LABEL__ must approve). new routine job load -> agent does directly.
- model: recurring/simple -> haiku (model routing).
- message-generation prompt must embed tone rules: __USER_LABEL__ address, polite form (no casual), no **, minimal symbols.

## procedure
1. check current time + timezone (`date "+%Z %z %H:%M"`, `readlink /etc/localtime`). macOS = Asia/Seoul KST = launchd local time base.
2. write plist — copy template `template.plist` then fill in:
   - Label: `com.telegram-skill-bot.telegram-agent.<name>` (kebab, time suffix e.g. retro-2100)
   - StartCalendarInterval Hour/Minute (Weekday if needed)
   - RunAtLoad=false (prevent immediate fire on load — first fire at next scheduled time)
   - ProgramArguments: push.sh --model haiku --prompt "<prompt with tone rules>"
   - logs: `runtime/logs/<name>.stdout.log` / `.stderr.log`
   - fill `__ROOT__` (repo root), `__HOME__` ($HOME), `__PATH__` (portable PATH) same as `__NAME__`/`__PROMPT__`/`__HOUR__`/`__MINUTE__`.
3. syntax check: `plutil -lint <plist>` -> confirm OK.
4. real test send: run push.sh with that prompt once directly to verify tone/content/delivery (no manual simulation).
5. pass -> register:
   ```bash
   cp routines/<plist> ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/<plist>
   launchctl list | grep <name>
   ```
6. atomically record in `routines.md` (date, Label, time, model, context).

## update / delete
- update: `launchctl unload <plist>` -> edit file -> reload.
- delete: `launchctl unload <plist>` then move plist with `trash` (avoid rm).

## registered crons
- retro-2100 — daily 21:00 KST retrospective (haiku, conversational). registered 2026-06-24.
- morning-brief-0600 — daily 06:00 KST adaptive morning briefing (haiku). routines/morning-brief.sh reads yesterday's unfinished items, today's tasks, today's appointments from Notion REST. registered 2026-06-24.
- weekly-review-sun2200 — weekly Sunday 22:00 KST weekly review (haiku). routines/weekly-review.sh: part 1 weekly briefing (this-week aggregate) + part 2 reflective review (good/bad moments, thoughts, encounters). registered 2026-06-24.
