---
name: dogany-portfolio-setup
display_name: 포트폴리오 설정
description: >-
  Conversational activation and soft migration of the universal portfolio
  index (core v1 -- one lint-checked table that tracks the estate's
  projects/units, plus a weekly reconcile pass). Fires when __USER_LABEL__
  says "set up a portfolio", "track my projects in one table", "turn the
  portfolio index on/off", "포트폴리오 설정해줘", "포트폴리오 인덱스 켜줘",
  "프로젝트 장부 만들어줘", or when a SessionStart signal says the portfolio
  offer is pending after onboarding. Two modes: fresh-mint offer (profile
  selection, index created only on opt-in) and soft migration (existing PM
  artifacts stay authoritative; guided, reversible cutover). Re-runnable
  anytime; records state in config/agent.conf (PORTFOLIO=pending/offered/
  on/off); registers the weekly reconcile routine via routine-ctl.sh when a
  declared-state column exists.
---

# dogany-portfolio-setup -- portfolio index conversational activation

Spec of record = `docs/PORTFOLIO-CORE.md` in the framework repo (dogany-agent
canonical). Working copy of the presets/option matrix = `presets.md` in this
skill folder (self-contained -- instances do not carry the framework docs/).
State = `PORTFOLIO=` key in `<agent-root>/config/agent.conf`.
Tooling (shipped with the framework, dormant until this skill activates it):
- lint: `python3 routines/lib/portfolio-core-lint.py <index>`
- parse entrypoint: `bash routines/lib/portfolio-core-parse.sh [index]`
- weekly reconcile pass: `bash routines/portfolio-reconcile.sh`
- self-test suite: `python3 routines/tests/test-portfolio-core.py`

## trigger signals
- SessionStart context: "portfolio pending" (after onboarding complete).
- user: "포트폴리오 설정해줘", "프로젝트 장부 만들어줘", "포트폴리오 꺼줘",
  "set up a portfolio index", "track my projects in one table".
- user with an EXISTING PM artifact set asks to adopt/migrate ("우리 방식을
  코어로 옮기자", "migrate my project tracking to the core schema").

## hard rules
- TIER-FREE (owner ruling dec-035, 2026-07-16): the portfolio module is PM
  hygiene, not a lifekit feature -- NO tier gate. Offer and activation are
  available on every tier including lite.
- FIRST ACTION when offering (pending state): set `PORTFOLIO=offered` in
  config/agent.conf BEFORE presenting the offer. One-shot: never auto-offer
  again; user can start anytime by asking. "Not now" -> leave `offered`.
  "No, never" -> set `PORTFOLIO=off`. Offer wording: i18n key
  `portfolio.offer` in `config/i18n/<AGENT_LANG>.json`.
- onboarding not finished (ONBOARDING_PENDING marker in AGENT.md) -> do NOT
  start portfolio setup; finish onboarding first.
- NEVER pre-create an empty scaffold. The index file is written ONLY on an
  explicit opt-in with a chosen profile. "None" is a legal answer that costs
  nothing (core C.3).
- SOFT MIGRATION IS NON-DESTRUCTIVE: existing PM artifacts (ticket dirs,
  backlog JSONs, hand-kept tables, narrative worklogs) stay AUTHORITATIVE and
  untouched until the operator explicitly says switch. A pre-existing index
  without a `core:` marker is a C=0 grandfather artifact -- structural lint +
  WARN only; that state is legal indefinitely.
- all edits idempotent: re-running setup must converge, never duplicate.
- one profile question at a time; short messages; numbered option lists end
  with the [[OPTIONS]] marker on the last line.

## procedure: fresh activation (no existing index)
1. read `presets.md`: present the four presets (derived-full / live-derived /
   manifest-min / curated-full) in ONE short numbered list, each with its
   one-line "matches you when ..." description, plus "compose options
   directly" and "none (skip)". Presets are conveniences, not a taxonomy.
2. on a preset (or composed) choice, confirm the option matrix values that
   differ from the preset default ONE at a time (persistence, existence
   writer, state machinery, edges, retirement level).
3. ask for the index path. Default: `product/PORTFOLIO.md` (keeps the
   shared-tooling default; the E9 header makes the file self-describing, so
   any path is legal). manifest-min fleet estates may prefer
   `product/instances.md` -- offer it as the alternative there.
4. write the index WITH the full E9 header contract (7 items, `core: 1`,
   `generated` stamp) from the preset template in `presets.md`. NEVER a bare
   table without the header.
5. self-test: run `python3 routines/tests/test-portfolio-core.py` once (suite
   must be green), then `python3 routines/lib/portfolio-core-lint.py <index>`
   on the new index (must PASS). Report both results in one line each.
6. escalation triggers (wire, do not preinstall):
   - the moment the index has any declared-state column (a `state` column, or
     any column classed `declared`): register the weekly reconcile cron --
     `bash routines/lib/routine-ctl.sh enable portfolio-reconcile
     routines/portfolio-reconcile.sh <HH:MM> <weekday>` (ask for day+time;
     default sun 18:00). Also scaffold
     `config/portfolio-reconcile.conf` with the estate's enumeration sources
     (dir-glob / file-glob / launchd-prefix -- see the reconcile script
     header) so the E1 existence diff is machine-runnable.
   - edges layer: offer ONLY on the E6 trigger (two or more cross-unit
     invariant classes, or a post-incident record). Never by default.
   - no declared-state column (manifest-min with state `-` everywhere and no
     declared columns): the reconcile cron still pays for itself (staleness +
     existence diff + disappearance report) -- offer it, user decides.
7. set `PORTFOLIO=on` in config/agent.conf. finish with a one-screen summary:
   profile, index path, header items, cron state.

## procedure: soft migration (existing PM artifacts present)
1. detect what exists (read-only survey): persisted index/table files, ticket
   dirs, backlog/state JSONs, narrative worklogs, scanner scripts.
2. match the estate to a preset and present the matching migration playbook
   from `presets.md` as a SHORT numbered step list. Say explicitly which
   artifacts stay authoritative (all of them, until cutover).
3. execute only the steps the operator approves, in order, each reversible:
   header contract first (or in the deriving tool's config for live-derived
   estates), enum mapping tables next (grandfather mode: report-only lint
   over legacy content, enforce-on-write for new writes), `core: 1` marker
   LAST -- the marker flip is the explicit, dated cutover and needs its own
   yes.
4. legacy index without `core:` marker stays legal forever (C0 grandfather).
   Never rush the flip; the burn-down count surfaces on the liveness terminal.
5. same escalation triggers as fresh activation (step 6 above).

## procedure: deactivate ("turn the portfolio off")
1. confirm once (one line). 2. `bash routines/lib/routine-ctl.sh disable
   portfolio-reconcile`. 3. set `PORTFOLIO=off`. NEVER delete the index file
   or any PM artifact -- data survives off; retirement of the index itself is
   the operator's manual act.

## notes
- speak outcomes, not file paths: say "portfolio index" / "weekly check",
  not script names, unless the user asks or a failure needs them.
- the index has ONE designated writer (this agent, per the header's
  update-authority tiers). Schema-tier changes (columns, class map) go
  through the tier-1 owner gate; row/edge content is tier-2 autonomous.
- model: conversation is the main agent itself; no delegation needed.
