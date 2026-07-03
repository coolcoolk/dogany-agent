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
5. finish with a one-screen summary: item -> state (+ schedule for routines).

## procedure: deactivate ("turn lifekit off")
1. confirm once (one line). 2. remove all bundle skill symlinks, routine-ctl
   disable all bundle routines. 3. set all BUNDLE_*=off, LIFEKIT=off.
   NEVER delete lifekit.db or skills-bundle/ contents -- data survives off.

## procedure: reconcile (conf hand-edited or drift suspected)
for each bundle.conf item: desired = lifekit.conf value, actual = symlink
exists / routine-ctl status. desired != actual -> apply desired. report diff.

## notes
- task-update skill is currently inert (lifekit task CLI not yet implemented)
  -- say so honestly when describing it: "prepared; activates when task
  tracking lands".
- model: conversation is the main agent itself; no delegation needed.
