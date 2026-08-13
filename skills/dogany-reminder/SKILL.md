---
name: dogany-reminder
display_name: 리마인더
description: One-shot (single-fire) reminders. Fires when the user asks to be reminded once at a future time — "remind me in 10 minutes", "remind me at 3pm", "tell me tomorrow at 9 to call the clinic". Korean triggers also fire it ("10분 뒤에 알려줘", "이따 2시에 전화하라고", "30분 뒤 알려줘"). Handles register / list / cancel. For RECURRING schedules use dogany-cron-register; for immediate push use dogany-proactive-push — this skill is strictly for a single delivery at a specific future time.
---

# dogany-reminder — one-shot (single-fire) reminder

Use when user wants single alert at specific future time ("remind me in 10 minutes").
At target time: one-shot scheduler job sends one message via push.sh, then self-removes (no leftovers, survives reboot).
Cross-platform: launchd on macOS, a transient systemd --user timer on Linux. reminder.sh detects the OS and picks the right mechanism; the assistant does not need to care which.

Portable / distributable: no hardcoded user name, absolute paths, or chat IDs.
Paths derive from script location and `$HOME`. Recipient + bot token come from `runtime/.env`.
User-facing strings (header, address term) come from `config/i18n/<lang>.json` (see config/agent.conf -> AGENT_LANG).

Core scripts: `<repo>/routines/reminder.sh` and `reminder-fire.sh`.
(Shared helpers: `routines/lib/agentlib.sh`.)

## trigger signals
- "remind me in X minutes/hours/days", "remind me at N o'clock", "tomorrow morning at 9 ...", and Korean equivalents above.
- one-time only. repeats every day/week -> NOT this skill — use dogany-cron-register.

## usage
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
<repo>/routines/reminder.sh cancel <[N]|N|label|all>
<repo>/routines/reminder.sh cancel all
```
Formats: `[N]` or bare `N` = index from list (easiest); `label` = full launchd label (backward compat); `all` = cancel everything.
`add` is optional: `reminder.sh "10m" "take meds"` also works. Does not echo label.

## how the assistant should operate
- extract time + content from user message and register directly. do not just hand over command — run it.
- time ambiguous ("later", "in a bit") -> ask once, then register. content empty -> ask briefly what to remind about.
- after registering, confirm in one line: when + what.
  e.g. "Set a reminder for 3:30pm to prep the meeting materials."
- delivered message auto-formatted as "<reminder_header>\n<content>"; header is localized. no extra tone shaping — pass content through as user gave it.
- "cancel that reminder" / "never mind" -> call `list`, relay reminders by INDEX or text only (never machine label com.telegram-skill-bot...), then cancel by index.
  Machine labels are internal only — user speaks by reminder text or index [1]/[2]/etc.

## mechanics (precision / durability)
- macOS: launchd StartCalendarInterval (Month/Day/Hour/Minute) -> minute-level precision. after firing, plist/meta/job self-remove.
- Linux: transient `systemd-run --user --on-calendar` timer (Persistent=true) -> minute-level precision, survives logout when linger is on (reminder.sh enables it best-effort). after firing, meta self-removes and the transient unit auto-vanishes.
- delays under 90s -> background sleep on BOTH OSes (sub-minute precision; not reboot-durable — fine for short ones).
- no TZ forced into job — fires in system local time, same clock target was computed against. Date parsing is portable (BSD `date -j` on macOS, GNU `date -d` on Linux).
- recipient/token resolved by push.sh from `runtime/.env`.
- meta files: `routines/.reminders/<label>.meta` (for list/cancel; deleted on fire).

## localization (i18n)
- strings in `config/i18n/<lang>.json`: `reminder_header`, `address`, etc.
- `config/agent.conf` -> `AGENT_LANG` picks locale (en, ko, ...).
- add language by dropping new `<lang>.json`; missing keys fall back to en.

## boundaries
- recurring schedules out of scope -> dogany-cron-register.
- never touch gateway / main bot. this skill only loads/removes its own one-shot jobs.
