---
name: dogany-lifekit-setup
display_name: 생활관리 설정
description: >-
  Install, remove, and on/off of the lifekit (life-management) kit, then
  hand over to the kit's own setup skill. Fires when __USER_LABEL__ says "set
  up life management", "생활관리 켜줘", "라이프킷 켜줘", "turn lifekit on/off",
  "라이프킷 꺼줘", "connect calendar" / "캘린더 연결", when a SessionStart
  signal says lifekit is pending after onboarding, or when the kit's own setup
  flow asks for a routine on/off or the Google connect. Install turns every
  kit skill on, shows the kit's offer, hands over to the kit's first-install
  flow. Records state in the kit conf (config/kit_life.conf, or
  config/lifekit.conf on older installs), links / unlinks skill dirs,
  (un)schedules routines via routine-ctl.sh. Re-runnable anytime; also
  reconciles a hand-edited kit conf ("apply lifekit settings").
---

# dogany-lifekit-setup -- kit on/off + hand-over

Framework skill = kit-agnostic activation. Everything about WHAT the kit
offers, which parts start when, times, copy, groups = kit-owned (kit setup
skill). Never name kit items or choose routine times here.

Paths differ per install (`kit_life` current, `lifekit` older mints). Resolve
first, never hardcode either name, never create a second conf (DGN-1676):
`python3 <agent-root>/routines/lib/life_engine.py <agent-root>` ->
`conf=` (kit conf: read AND write here), `bundle=` (empty -> kit not
installed: install step 0 installs it). Bundle definition (PRIMARY SOURCE) =
`bundle=` path. State = kit conf.
Offer text = the kit's key `life_setup.offer`:
`python3 <agent-root>/routines/lib/life_engine.py <agent-root> --offer`
(kit key missing -> framework offer-fallback i18n key in agent lang ->
built-in English default). Speak it verbatim.
Kit setup flow = `.claude/skills-bundle/brief-setup/first-install.md`;
absent -> `.claude/skills-bundle/life-setup/first-install.md` (older kit).
Kit owned: copy, group pick, every step after the pick.

## trigger signals
- SessionStart context: "lifekit pending" (after onboarding complete).
- user: "생활관리 켜줘/설정해줘", "라이프킷 꺼줘", "set up life management",
  "캘린더 연결".
- kit setup flow: routine on/off, Google connect.
- user hand-edited kit conf -> "apply lifekit settings" -> reconcile.
- time / schedule changes, per-item choices: NOT here -- the kit's setup
  skill owns them.

## hard rules
- GATE (same as the onboarding offer, onboarding-check.py): activation needs
  `KIT=` in config/agent.conf naming the life kit (`kit_life` | `lifekit`).
  Main (`DOGANY_AGENT_CLASS` in `<agent-root>/.instance.conf`, missing =
  main) with KIT= pending / none / absent AND an explicit __USER_LABEL__
  request to turn lifekit on -> run the kit-select step first (same writes
  as the onboarding kit-select answer: kit conf `LIFEKIT=pending`
  write-if-absent FIRST, then `KIT=lifekit` in config/agent.conf LAST), then
  continue with install. Domain without that KIT= value -> do not activate;
  point the user to their main agent. Never select the kit unasked.
  Gate applies to NEW activation only: `LIFEKIT=on` is never deactivated by
  config; deactivation only on user request.
- FIRST ACTION on the pending signal: install step 0. It sets
  `LIFEKIT=offered` only after the kit files are really there; a failed
  install leaves `LIFEKIT` untouched so the offer is not spent. Never write
  `LIFEKIT=offered` by hand. One-shot: never auto-offer again.
- onboarding not finished (ONBOARDING_PENDING marker in
  identity/hot.custom.agent.md) -> do NOT start lifekit setup; finish
  onboarding first.
- never send routine test-fire pushes (noisy). Verification = routine-ctl.sh
  exit code + status.
- all edits idempotent: re-running converges, never duplicates.

## procedure: install
0. kit files (DGN-1817):
   `bash <framework>/scripts/pack/kit_first_install.sh install <agent-root>`
   (`<framework>` = `DOGANY_REPO_ROOT` in `<agent-root>/.instance.conf`).
   It installs the kit when `bundle=` is empty (pack_install.sh, source and
   tag from packs/catalog.json only), re-reads `bundle=`, and only then sets
   `LIFEKIT=pending` -> `offered`. Never install by hand, never pick another
   source. rc 0 -> continue with the printed `bundle=`. rc 3 (kit source not
   reachable) or 4 (install failed) -> tell __USER_LABEL__ WHY, verbatim from
   `config/i18n/<AGENT_LANG>.json` (owner-confirmed copy, dec-229): rc 3 +
   `kind=network` -> `lifekit.install_failed.network`; rc 3 + `kind=no_version`
   -> `lifekit.install_failed.no_version`; rc 4 -> `lifekit.install_failed.error`.
   Then `lifekit.install_failed.tail`, except after `.network` (it already
   asks to retry). Never paraphrase, never show the machine reason; stop.
   LIFEKIT stays as it was, so the offer comes back later.
1. read bundle.conf (skip comment lines AND directive lines starting with
   `@`; TAB fields: id, kind, schedule, desc_key, module, optional slot).
   Read directives only for `@always-on<TAB><id>`: kit skill on whenever
   LIFEKIT=on (no row, no BUNDLE_ key). Ignore `@domain-subset` lines
   (installer-owned).
2. every `kind=skill` row AND every `@always-on` id: skill on (below).
   Routine rows: untouched -- the kit flow turns them on when needed.
3. kit conf: `BUNDLE_<ID>=on` per skill row (none for `@always-on` ids),
   `LIFEKIT=on`.
4. on FIRST activation (LIFEKIT transitions to `on`): the kit owns the role
   line. (a) run
   `bash <framework>/scripts/pack/mint_run.sh stamp-role --root <agent-root> --role "<kit role line>"`
   with the kit's own declared role line for `AGENT_LANG` (kit-side copy,
   never composed here). No-op when the slot already holds a role. No
   declared line -> skip (a). (b) extend the Role section of
   identity/hot.custom.agent.md with a staff-agent orchestration bullet --
   coordinating staff agents / domain lanes on top of the general role.
   Idempotent: skip if already present. On deactivation do NOT auto-remove
   either; trim only when the user asks.
5. hand over: show the kit offer (`--offer` above, verbatim), then follow the
   kit setup flow from its offer step. The kit drives everything after the
   pick and calls back here for routine on/off and Google connect. Flow absent
   (older kit) -> say the offer text, stop. No per-item walk, no summary screen.

## procedure: skill / routine on-off (user request or kit flow)
- skill on:  `ln -s ../skills-bundle/<id> <agent-root>/.claude/skills/<id>`
  (skip if link already exists). Kit install delivers every kit skill body,
  even when off; link alone turns it on. Never reinstall for skill on.
  Body missing (`.claude/skills-bundle/<id>/SKILL.md` absent: domain instance
  with only its kit subset, or damaged install) -> do NOT create a dangling
  link; tell __USER_LABEL__ the item needs a kit update first; stop for that
  item.
  skill off: remove that symlink ONLY (never touch
  `.claude/skills-bundle/<id>` -- real data lives there). `@always-on` ids:
  never switch off one by one (only by deactivate).
- routine on:  `bash routines/lib/bundle-routine-ctl.sh on <id> ["<[dow ]HH:MM>"]`
  (time omitted = bundle.conf schedule; `HH:MM` daily, `<dow> HH:MM` weekly).
  The helper schedules the routine and handles briefing-slot exclusion itself
  (a slot-owning routine unschedules the same-slot generic-brief).
- routine off: `bash routines/lib/bundle-routine-ctl.sh off <id>` -- unschedules
  the routine and restores its slot's generic-brief.
- Slot ownership = the kit's bundle.conf `slot` column; the helper reads it.
  Never name or map routine ids here, never call brief-slot-ctl.sh by hand.
- Exit: 0 ok (a `WARN` row = swap failed, routine state already set: tell
  __USER_LABEL__ the briefing may double or go quiet, then reconcile) | 1 bad
  id / schedule / bundle.conf slot column -> nothing changed, stop | 4
  routine-ctl.sh failed -> nothing swapped, stop.
- The swap is launchd-load-layer ONLY -- never delete the generic-brief
  plist file. Idempotent: re-running converges.
- record in kit conf (`BUNDLE_<ID>=on|off`, `-` -> `_`, uppercase).

## procedure: deactivate ("turn lifekit off")
1. confirm once (one line). 2. remove all bundle skill symlinks
   (skill rows and `@always-on` ids),
   `bundle-routine-ctl.sh off <id>` every bundle routine (slot-owning ones
   restore their generic-brief, so briefings do not go dark).
   3. set all BUNDLE_*=off, LIFEKIT=off.
   NEVER delete the kit db (database/<stem>.db) or skills-bundle/ contents -- data survives off.

## procedure: reconcile (conf hand-edited or drift suspected)
for each bundle.conf item (skip `@` lines as in install step 1): desired =
kit conf value. For each `@always-on` id: desired = on iff LIFEKIT=on.
actual = symlink exists / routine-ctl status. desired != actual -> apply
desired (routine: `bundle-routine-ctl.sh on|off <id>`). report diff.
Slot invariant: for every routine row whose state already matched, run
`bundle-routine-ctl.sh slot-sync <id>` (re-aligns slot to actual state, never
re-renders its clock; repairs double or missing briefing). Slotless rows: no-op.

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
kit conf as `MIRROR_CAL_NAME=<name>`. This name becomes the
display PREFIX of the category calendars created in step 5 (e.g.
"<name>-<category>"). NOTE: `MIRROR_TASKLIST_NAME` is DEPRECATED for this
path -- the checklist display name comes from the same prefix + i18n
(`mirror.gtasks.checklist`); `MIRROR_TASKLIST_NAME` is still honored on
legacy single-calendar instances but silently unused in multi mode.

### step 5 -- engrave (provision the mirror surfaces)
Call `adapter.ensure_mirror_engraved(state)` (DGN-364). It routes by the
resolver outcome (normative M3 table):

- Targets already resolve (multi or legacy) -> no-op, proceed.
- Nothing engraved (fresh mint) -> `provision_category_calendars`: creates
  the 3 category calendars (category names come from the mirror adapter, named
  "<MIRROR_CAL_NAME>-<category>") + the checklist tasklist, engraves the 4
  ids. Fresh mints default to MULTI (dec-033).
- Partial V15 engrave -> provision again (idempotent; fills only holes).
- Partial LEGACY engrave (one agent_* key lost) -> `bootstrap()` repairs in
  legacy mode; provision is NEVER used as a legacy repair (it would orphan
  the live calendar and duplicate rows).

Two outcomes:

- Clean -> proceed.
- `adapter.BootstrapAmbiguous(candidates)` raised -> a single run can
  surface up to FOUR adopt candidates (three category calendars + one
  tasklist); the exception carries the FULL candidate set. Show ALL of them
  (each has surface, candidate_id, summary -- existing same-name surfaces
  without our marker) and ask ONE question covering the whole set: "adopt
  these existing ones, or create new ones under a different name?" Do not
  assume a single candidate.
  - adopt -> set `MIRROR_ADOPT_UNMARKED=true` in kit conf,
    re-run `ensure_mirror_engraved`. On success, immediately unset it (set
    back to `false` or remove the line). It is a one-shot answer: leaving it
    `true` permanently would silently auto-adopt same-name foreign surfaces
    in the future (e.g. if a marked calendar is deleted and a new same-name
    one exists).
  - create -> ask for a new name, update `MIRROR_CAL_NAME`, re-run
    `ensure_mirror_engraved`.

### step 6 -- enable mirror module
Set `MIRROR_MODULE=on` in kit conf.

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

### step 8 -- first backfill + outbound backlog + self-test
Run bounded initial pull (backfill). Then push items created before connect,
once (write-time enqueue is a no-op while the mirror is off; without this
they wait for the weekly reconcile):
`python3 mirror/reconcile.py --repair --drain`. Then:
1. Create a test event.
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
- model: conversation is the main agent itself; no delegation needed.
- display names: skill folder IDs are machine IDs, never speak them to the
  user. Use the localized display name (i18n `bundle.<id>.name`). The bridge
  /skills command renders these automatically; follow the same rule in any
  dialogue you produce.
