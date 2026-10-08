---
name: dogany-cron-register
display_name: 정기 루틴 등록
description: When __USER_LABEL__ hands off a recurring job (cron / regular routine), the agent registers it end-to-end using the OS-native scheduler (macOS launchd / Linux systemd --user timer). Fires on requests like "do X every day at Nam", "run this regularly", "set up a cron", "make this a routine". The agent writes the unit, validates it, sends a test fire, and loads it - it does the whole thing rather than dumping code on __USER_LABEL__. Only exception: restarting the gateway / main bot (ask __USER_LABEL__ for that).
---

# dogany-cron-register — recurring task registration (launchd / systemd)

__USER_LABEL__ assigns recurring task -> agent registers it end-to-end. macOS = launchd plist; Linux = systemd --user timer. (dogany-skill-creator convention output.)

## trigger signals
- "매일/매주 몇시에 X 해줘", "정기적으로", "크론 걸어줘", "루틴으로 만들어"

## routing gate -- NOT this skill for user life schedules
- cron/plist is ONLY for AGENT-SIDE system jobs: briefings, sweeps, syncs, notifications the agent fires autonomously.
- if the recurring thing is __USER_LABEL__'s life schedule (commute, workout, meal, sleep, any human-calendar event they want visible/tracked), do NOT register a cron. route to the lifekit routine engine: `lifekit.sh routine add ...` (requires lifekit routine machinery).
- litmus: "Would __USER_LABEL__ expect to SEE this on their calendar?" -> lifekit routine, not cron.

## core rules
- do not just hand over code to __USER_LABEL__ — agent registers end-to-end.
- exception: gateway/main bot restart/stop -> do not do alone (__USER_LABEL__ must approve). new routine job load -> agent does directly.
- model: recurring/simple -> haiku (model routing).
- message-generation prompt must embed tone rules: __USER_LABEL__ address, polite form (no casual), no **, minimal symbols.
- pick the mechanism by OS: `uname -s` = Darwin -> launchd (procedure A); else -> systemd --user timer (procedure B). Same push.sh command either way.
- conditional send: job that should push only when a condition holds -> wrap in a script (check condition; silent exit when nothing to send). deterministic body may use `push.sh --text` directly (no model call). ProgramArguments / ExecStart then point at the wrapper, not push.sh.
- worker-script pattern: when the job's primary body is real work (data processing, report generation, sweeps) and push.sh is secondary, ProgramArguments / ExecStart point at the worker script, not push.sh. the script does the work, then calls `push.sh --text` only when a message is warranted. this is the standard shape for conditional/quiet crons; the conditional-send clause above is a special case of it.
- unattended throttling guard: any unattended cron that invokes headless claude (`claude -p`) MUST set `ProcessType=Interactive` in its plist (launchd-native fix: App Nap / timer coalescing throttles background jobs when display off + no input, even with pmset sleep=0; template.plist ships the key). secondary option: caffeinate -- if used, wrap with `caffeinate -i` (add `-s` for lid-closed laptop on AC); do NOT use `-d` or `-m`.

## procedure A -- macOS (launchd)
1. check current time + timezone (`date "+%Z %z %H:%M"`, `readlink /etc/localtime`). launchd fires in system local time.
2. write plist — copy template `template.plist` then fill in:
   - Label: `com.telegram-skill-bot.telegram-agent.<name>` (kebab, time suffix e.g. retro-2100)
   - StartCalendarInterval Hour/Minute (Weekday if needed)
   - RunAtLoad=false (prevent immediate fire on load — first fire at next scheduled time)
   - ProgramArguments: push.sh --model haiku --prompt "<prompt with tone rules>"
   - logs: `runtime/logs/<name>.stdout.log` / `.stderr.log`
   - instance variables: label middle segment (`telegram-agent`) and log dir (`runtime/logs/`) vary per instance -- match the instance's existing convention (check loaded labels / routines.md), do not assume these defaults.
   - fill `__ROOT__` (repo root), `__HOME__` ($HOME), `__PATH__` (portable PATH) same as `__NAME__`/`__PROMPT__`/`__HOUR__`/`__MINUTE__`.
3. syntax check: `plutil -lint <plist>` -> confirm OK.
4. real test send: run push.sh with that prompt once directly to verify tone/content/delivery (no manual simulation).
   - update exception: MAY skip the test fire ONLY when ALL hold: (a) schedule-only change (prompt / logic / command line unchanged), (b) same-day successful run evidence of the identical command, (c) plist lint (`plutil -lint`) + load verification still performed. otherwise test fire stays mandatory.
5. pass -> register:
   ```bash
   cp routines/<plist> ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/<plist>
   launchctl list | grep <name>
   ```
6. atomically record in `routines.md` (date, Label, time, model, context).

## procedure B -- Linux (systemd --user timer)
1. check current time + timezone (`date "+%Z %z %H:%M"`, `timedatectl` if present). systemd OnCalendar fires in system local time.
2. write a .service + .timer under `~/.config/systemd/user/` (name `dogany-<name>`):
   - `dogany-<name>.service`: `[Service] Type=oneshot`, `ExecStart=/bin/bash <ROOT>/routines/push.sh --model haiku --prompt "<prompt with tone rules>"`, `Environment=HOME=$HOME`, `WorkingDirectory=<ROOT>`.
   - `dogany-<name>.timer`: `[Timer] OnCalendar=<schedule>` (systemd.time(7): e.g. daily `*-*-* 21:00:00`; Sunday `Sun *-*-* 22:00:00`), `Persistent=true`, `[Install] WantedBy=timers.target`.
3. syntax check: `systemd-analyze --user verify ~/.config/systemd/user/dogany-<name>.timer` (or `systemctl --user cat`) -> confirm no errors.
4. real test send: run the same push.sh command once directly to verify tone/content/delivery.
   - update exception: same conditions as procedure A step 4 (verification = `systemd-analyze --user verify` + timer active check instead of plist lint).
5. pass -> register + verify:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now dogany-<name>.timer
   systemctl --user is-enabled dogany-<name>.timer
   loginctl enable-linger "$USER"   # survive logout/reboot
   ```
6. atomically record in `routines.md` (date, unit name, OnCalendar, model, context).

## update / delete
- time change -> RENAME, not edit-in-place: rename label + script + log filenames to the new time suffix (create new unit, load it, move the old plist / script / logs to trash -- trash, never rm). keeping the same label is allowed ONLY when the time is unchanged.
- macOS update: `launchctl unload <plist>` -> edit file -> reload. delete: `launchctl unload <plist>` then move plist with `trash` (avoid rm).
- Linux update: edit the .service/.timer -> `systemctl --user daemon-reload` -> `systemctl --user restart dogany-<name>.timer`. delete: `systemctl --user disable --now dogany-<name>.timer` then move the unit files with `trash`.

## registered crons
- Per-instance registrations live in the instance's `routines.md` (step 6),
  never in this shared skill file.
- Lifekit bundle routines (morning-brief, daily-retro) are NOT registered via
  this skill: the dogany-lifekit-setup skill schedules them through the
  non-conversational helper `routines/lib/routine-ctl.sh` (idempotent, no
  test-fire). Use this skill only for ad-hoc recurring jobs the user asks for.
