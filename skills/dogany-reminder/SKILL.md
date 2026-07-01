---
name: dogany-reminder
description: One-shot (single-fire) reminders. Fires when the user asks to be reminded once at a future time — "remind me in 10 minutes", "remind me at 3pm", "tell me tomorrow at 9 to call the clinic". Korean triggers also fire it ("10분 뒤에 알려줘", "이따 2시에 전화하라고", "30분 뒤 알려줘"). Handles register / list / cancel. For RECURRING schedules use dogany-cron-register; for immediate push use dogany-proactive-push — this skill is strictly for a single delivery at a specific future time.
---

# dogany-reminder — one-shot (single-fire) reminder

Use when the user wants a single alert at a specific future time ("remind me in
10 minutes"). At the target time a one-shot launchd job sends one message via
push.sh, then removes itself (no leftovers, survives reboot).

Portable / distributable: no hardcoded user name, absolute paths, or chat IDs.
Paths derive from the script location and `$HOME`. The recipient and bot token
come from `runtime/.env`. User-facing strings (the header, the address
term) come from `config/i18n/<lang>.json` (see config/agent.conf → AGENT_LANG).

Core scripts: `<repo>/routines/reminder.sh` and `reminder-fire.sh`.
(Shared helpers: `routines/lib/agentlib.sh`.)

## Trigger signals
- "remind me in X minutes/hours/days", "remind me at N o'clock", "tomorrow
  morning at 9 ...", and the Korean equivalents above.
- One-time only. If it repeats every day/week, this is NOT the skill — use
  dogany-cron-register.

## Usage
Relative time (s / m / h / d, combinable):
```bash
<repo>/routines/reminder.sh add "10m" "take meds"
<repo>/routines/reminder.sh add "1h30m" "take the laundry out"
```
Absolute time (today HH:MM, rolls to tomorrow if already past):
```bash
<repo>/routines/reminder.sh add "15:30" "prep meeting materials"
```
Explicit timestamp:
```bash
<repo>/routines/reminder.sh add "2026-06-28 09:00" "confirm clinic appointment"
```
List / cancel:
```bash
<repo>/routines/reminder.sh list
<repo>/routines/reminder.sh cancel <label>     # label shown by `list`
<repo>/routines/reminder.sh cancel all
```
`add` is optional: `reminder.sh "10m" "take meds"` also works.

## How the assistant should operate
- Extract time + content from the user's message and register it directly. Do
  not just hand over the command — run it.
- If the time is ambiguous ("later", "in a bit"), ask once, then register. If
  the content is empty, ask briefly what to remind about.
- After registering, confirm in one line: when + what.
  e.g. "Set a reminder for 3:30pm to prep the meeting materials."
- The delivered message is auto-formatted as "<reminder_header>\n<content>";
  the header is localized. No extra tone shaping needed — pass the content
  through as the user gave it.
- On "cancel that reminder" / "never mind", find the label via `list`, then cancel.

## Mechanics (precision / durability)
- launchd StartCalendarInterval (Month/Day/Hour/Minute) → minute-level
  precision. After firing, the plist/meta/job self-remove.
- Delays under 90s use a background sleep (sub-minute precision; not
  reboot-durable — fine for short ones).
- No TZ is forced into the job — it fires in system local time, the same clock
  the target was computed against.
- Recipient/token resolved by push.sh from `runtime/.env`.
- Meta files: `routines/.reminders/<label>.meta` (for list/cancel; deleted on fire).

## Localization (i18n)
- Strings live in `config/i18n/<lang>.json`: `reminder_header`, `address`, etc.
- `config/agent.conf` → `AGENT_LANG` picks the locale (en, ko, ...).
- Add a language by dropping a new `<lang>.json`; missing keys fall back to en.

## Boundaries
- Recurring schedules are out of scope → dogany-cron-register.
- Never touch the gateway / main bot. This skill only loads/removes its own
  one-shot jobs.
