---
name: dogany-lifekit-setup
description: >-
  Conversational activation, deactivation, and modification of the lifekit
  (life-management) default bundle. Fires when __USER_LABEL__ says "set up life
  management", "turn lifekit on/off", "enable the diet/workout/appointment
  skills", "change my morning briefing time", "stop the daily retro", or when a
  SessionStart signal says lifekit is pending after onboarding. The agent walks
  through the bundle items one at a time (skills first, then scheduled
  routines), records choices in config/lifekit.conf, links/unlinks skill dirs,
  and (un)schedules routines via routine-ctl.sh. Re-runnable anytime; also
  reconciles hand-edited lifekit.conf ("apply lifekit settings").
---

# dogany-lifekit-setup -- lifekit bundle conversational activation

Bundle definition (PRIMARY SOURCE) = `<agent-root>/service/lifekit/bundle.conf`.
State = `<agent-root>/config/lifekit.conf`. Item descriptions = i18n keys
(`bundle.<id>`) in `config/i18n/<AGENT_LANG>.json`; the initial offer wording
comes from the `lifekit.offer` key in the same file.

## trigger signals
- SessionStart context: "lifekit pending" (after onboarding complete).
- user: "생활관리 켜줘/설정해줘", "라이프킷 꺼줘", "아침 브리핑 시간 바꿔줘",
  "set up life management", "turn off the daily retro".
- user hand-edited lifekit.conf -> "apply lifekit settings" -> reconcile.

## hard rules
- TIER GATE (check FIRST, before any offer or activation): read `DOGANY_TIER`
  from `<agent-root>/.instance.conf` (missing file or field = lite). If lite:
  do NOT offer or activate anything. Reply with ONE short line -- the lifekit
  bundle (tracking skills + scheduled routines) lives in the CRAFT (basic)
  tier -- then move on. Never repeat this upsell unprompted. The gate applies
  to NEW activation only: items already active (`LIFEKIT=on`) are never
  deactivated by tier; deactivation happens only when the user asks.
- FIRST ACTION when offering (pending state): set `LIFEKIT=offered` in
  lifekit.conf BEFORE presenting the offer. One-shot: never auto-offer again;
  user can start anytime by asking. If user says "not now" -> leave `offered`.
  If user says "no, never" -> set `LIFEKIT=off`.
- onboarding not finished (ONBOARDING_PENDING marker in AGENT.md) -> do NOT
  start lifekit setup; finish onboarding first.
- never send routine test-fire pushes during activation (noisy). Verification
  = routine-ctl.sh exit code + status.
- all edits idempotent: re-running setup must converge, never duplicate.

## procedure: activate / modify
1. read bundle.conf (skip comment lines; fields: id, kind, schedule, desc_key).
2. read current lifekit.conf state. present items ONE at a time, short
   description from i18n `bundle.<id>` (fallback: describe from the skill's
   own SKILL.md in one line). skills first, then routines.
   - numbered options per item (on / off / for routines also "different time").
   - routines: confirm schedule (default from bundle.conf, user may change).
3. after each answer, apply immediately:
   - skill on:  `ln -s ../skills-bundle/<id> <agent-root>/.claude/skills/<id>`
     (skip if link already exists). skill off: remove that symlink ONLY (never
     touch `.claude/skills-bundle/<id>` -- real data lives there).
   - routine on:  `bash routines/lib/routine-ctl.sh enable <id> routines/bundle/<id>.sh <HH:MM>`
   - routine off: `bash routines/lib/routine-ctl.sh disable <id>`
4. record each choice in lifekit.conf (`BUNDLE_<ID>=on|off`, `-` -> `_`,
   uppercase). when at least one item is on -> `LIFEKIT=on`; user declined
   everything -> `LIFEKIT=off`.
5. on FIRST activation (LIFEKIT transitions to `on`): extend the Role section
   of AGENT.md with the CRAFT role -- append one bullet adding domain-agent
   orchestration (coordinating specialist agents / domain lanes) on top of
   the general role. Idempotent: skip if already present. On deactivation do
   NOT auto-remove it; trim only when the user asks.
6. finish with a one-screen summary: item -> state (+ schedule for routines).

## procedure: deactivate ("turn lifekit off")
1. confirm once (one line). 2. remove all bundle skill symlinks, routine-ctl
   disable all bundle routines. 3. set all BUNDLE_*=off, LIFEKIT=off.
   NEVER delete lifekit.db or skills-bundle/ contents -- data survives off.

## procedure: reconcile (conf hand-edited or drift suspected)
for each bundle.conf item: desired = lifekit.conf value, actual = symlink
exists / routine-ctl status. desired != actual -> apply desired. report diff.

## Connect Google (calendar + tasks + email)

Walk user through this conversationally, one step at a time. Agent drives;
user runs only their own auth commands (per RULES: hand OAuth commands to
user, do not run them).

### step 1 -- preflight
Run `routines/mirror-setup-check.sh --quiet`. Report each item: OK or MISSING.
Items checked: (a) gws CLI present, (b) gws auth OK + scopes include
calendar+tasks+gmail.send, (c) python cryptography importable.

### step 2 -- install gaps (agent runs these, not auth commands)
- gws missing -> tell user: `npm i -g @googleworkspace/gws`
- cryptography missing -> `python3 -m pip install cryptography`
Re-run preflight after installs to confirm.

### step 3 -- auth grant (user runs, agent verifies)
Hand the user these two commands in order:
```
gws auth setup
gws auth login --scopes https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/tasks,https://www.googleapis.com/auth/gmail.send
```
Do NOT run them yourself. `--scopes` passes exact scope URLs; `-s/--services`
only filters the picker by service name and does NOT pin exact scopes ("gmail.send"
is not a valid service name). The exact URL list above is what the preflight
greps for -- they must match verbatim. After user confirms done, agent runs
`routines/mirror-setup-check.sh` (full check: calendar+tasks+gmail.send) to
verify scopes pass.

### step 4 -- calendar name
Ask user: "What name should your calendar have?" Write the answer to
`config/lifekit.conf` as `MIRROR_CAL_NAME=<name>`. Tasklist defaults to same
name (no separate key needed unless user picks differently).

### step 5 -- bootstrap
Call the mirror bootstrap. Two outcomes:

- Clean -> proceed.
- `adapter.BootstrapAmbiguous(candidates)` raised -> show the candidate(s)
  (each has surface, candidate_id, summary -- an existing same-name
  calendar/tasklist without our marker). Ask: "adopt this existing calendar,
  or create a new one with a different name?"
  - adopt -> set `MIRROR_ADOPT_UNMARKED=true` in `config/lifekit.conf`,
    re-run bootstrap. On success, immediately unset it (set back to `false`
    or remove the line). It is a one-shot answer: leaving it `true` permanently
    would silently auto-adopt a same-name foreign calendar in the future (e.g.
    if the marked calendar is deleted and a new same-name one exists).
  - create -> ask for a new name, update `MIRROR_CAL_NAME`, re-run bootstrap.

### step 6 -- enable mirror module
Set `MIRROR_MODULE=on` in `config/lifekit.conf`.

### step 7 -- enable crons
- macOS: `launchctl load` both mirror plists:
  `routines/com.telegram-skill-bot.<agent>.mirror-poll.plist`
  `routines/com.telegram-skill-bot.<agent>.mirror-reconcile.plist`
- Linux: systemd cannot enable units by name when they sit in routines/.
  First copy (or symlink) them into the user unit dir, then enable. Unit files
  carry substituted absolute paths already (mint/update bakes them in), so a
  plain copy is correct.
  ```
  mkdir -p ~/.config/systemd/user
  cp routines/com.telegram-skill-bot.<agent>.mirror-poll.service routines/com.telegram-skill-bot.<agent>.mirror-poll.timer ~/.config/systemd/user/
  cp routines/com.telegram-skill-bot.<agent>.mirror-reconcile.service routines/com.telegram-skill-bot.<agent>.mirror-reconcile.timer ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now com.telegram-skill-bot.<agent>.mirror-poll.timer
  systemctl --user enable --now com.telegram-skill-bot.<agent>.mirror-reconcile.timer
  ```
Poll = every 300s. Reconcile = weekly Sun 21:30.

### step 8 -- first backfill + self-test
Run bounded initial pull (backfill). Then:
1. Create a lifekit test event.
2. Run one poll cycle manually.
3. Verify the test event appears on the connected calendar surface.
4. Delete the test event.

### step 9 -- report
Tell user: "Connected. N items synced." (N = count from backfill output.)

### hard rules for this flow
- One step at a time. Confirm each before proceeding.
- Never run `gws auth` commands yourself -- hand them to the user.
- `MIRROR_ADOPT_UNMARKED=true` must only be set after explicit user choice
  to adopt. Default: absent (safe, never auto-adopts).

## notes
- task-update skill is currently inert (lifekit task CLI not yet implemented)
  -- say so honestly when describing it: "prepared; activates when task
  tracking lands".
- model: conversation is the main agent itself; no delegation needed.
- display names: skill folder IDs (diet-log, workout-log, etc.) are machine
  IDs, never speak them to the user. Use the localized display name instead
  (e.g. say "식단 기록" not "diet-log", "운동 기록" not "workout-log"). The
  bridge /skills command renders these automatically; follow the same rule in
  any dialogue or summary you produce.
