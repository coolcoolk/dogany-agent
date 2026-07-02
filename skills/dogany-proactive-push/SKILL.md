---
name: dogany-proactive-push
description: Proactively send a message to __USER_LABEL__'s Telegram (outbound push initiated by the agent, with no one asking first). Use for scheduled routines (briefing / retro / reminder) or task-completion notifications. Sends content verbatim (--text) or generates it with a chosen model (--prompt --model). This is the core part invoked from cron / launchd.
---

# dogany-proactive-push — outbound Telegram push

Bot sends message to __USER_LABEL__ Telegram first (bot token + chat_id read from `.telegram_bot/.env`).
Core script: `routines/push.sh`.

## when to use
- fire recurring routine (daily retro, diet briefing, etc.) from cron/launchd
- notify __USER_LABEL__ of long task completion or failure
- anything worth alerting __USER_LABEL__ without waiting for request (keep frequency low — avoid notification fatigue)

## usage

send as-is (no claude, immediate):
```bash
routines/push.sh --text "message text"
```

generate via model then send (route by difficulty):
```bash
routines/push.sh --model haiku  --prompt "오늘 일일 회고 질문 하나 던져줘"
routines/push.sh --model sonnet --prompt "아래 데이터를 마아서 주간 요약 만들어줘: ..."
```

## model selection guide (cost lever)
- `haiku` — simple recurring routines, fixed tone (default)
- `sonnet` — data wrangling, medium complexity (summarize/aggregate)
- `opus` — hard reasoning notifications (rare)

## notes
- Telegram 4096-char limit -> script auto-cuts at 4000.
- sends as plain text (no markdown parsing) — prevents rendering breakage.
- bot token + chat_id read from `.telegram_bot/.env`.
- routine channel split (design A/B) confirmed -> `--thread <id>` arg to be added.
- exit codes: 0 success / 1 config error / 2 Telegram send failure.
