---
name: dogany-skill-creator
display_name: 스킬 만들기
scope: framework
layer: shipped
description: Meta-skill. Create and update skills THIS system's way. Triggers: user says make / skillify / update a skill; agent packages a repeated procedure (cron / routine / workflow). Read before ANY skill / cron / routine. Enforces asking the USER for scope BEFORE authoring (location + activate_for; no answer = no skill), naming, path, caveman-English body, model routing, tests.
---

# skill-creator -- skill creation + update standard

Stop drift. Read before ANY skill / cron / routine. THIS system's rules, not
generic. Self-obeys: flow first, rules after; steps declared in product.yaml
`lanes:`.

Step order below = `after:` in product.yaml. `after:` is a declaration only:
nothing enforces step order yet (needs the DGN-943 step rail).

## flow -- new skill

| id | step | lane | kind | after |
|----|------|------|------|-------|
| s1 | ask location | llm | - | - |
| s2 | record declaration | rule | gate | s1 |
| s3 | memory or skill | llm | - | s2 |
| s4 | write SKILL.md | llm | - | s3 |
| s5 | aux scripts / templates | llm | - | s4 |
| s6 | register local layer | rule | value | s4 |
| s7 | real run test | llm | - | s5 |
| s8 | final lint | rule | verify | s6, s7 |
| s9 | report + worklog note | llm | - | s8 |

Rule-step carrier = `steps.sh <verb>` in this folder, a thin wrapper over
routines/skill-landing-gate.py + routines/skill-layer.py. Instance root =
`$DOGANY_ROOT`, else cwd.

s1 ask location -- HARD STOP until answered.
- ask the USER, in their language, two questions:
  (1) location -- local (this instance only) | shared (machine-wide
      incubation tier) | framework (product canonical, release-borne).
  (2) activate_for -- which other agents get it activated (default none;
      location local -> none).
- answer also fixes layer: framework -> `layer: shipped`; local | shared ->
  `layer: local`.
- no answer -> stop. no folder, no SKILL.md.
- own classification is not an answer. classes (rules: routing) frame the
  question only. ask every time; leave the choice to the USER.

s2 record declaration:
- write `.claude/.skill-landing-declared`: `skill: <name>` /
  `location: <answer>` / `activate_for: <answer>`.
- run `steps.sh declare-check <skill-dir>`. exit 0 = clear; exit 1 = reason
  printed -> fix declaration, rerun.
- tier GUARANTEED: hook routines/skill-landing-gate.py (PreToolUse on
  Write/Edit/MultiEdit; DGN-1394 + DGN-1476) denies the write creating a new
  SKILL.md without a fresh declaration naming that skill.
- hook proves the declaration exists, not that you asked -- a self-filled
  declaration is a defect.

s3 memory or skill -- see rules: memory vs skill. memory -> stop here.

s4 write SKILL.md -- folder + SKILL.md. frontmatter, in order:
- `name:` kebab-case, verb-ish.
- `display_name:` 2-4 word noun phrase, user language (Korean for
  Korean-persona instances). speak this to the user, never the folder ID.
- `description:` see rules: description.
- `scope:` = s1 location, verbatim. durable copy: declaration expires,
  frontmatter stays. hook denies a Write whose `scope:` is missing or
  disagrees with the declaration.
- `layer:` = s1 layer.
Body follows rules: language + tokens, body conventions.

s5 aux scripts / templates -- same folder as SKILL.md.

s6 register local layer -- layer local only: `steps.sh register <skill-dir>`
(calls `routines/skill-layer.py register`). layer shipped -> skip.

s7 real run test -- run it for real, not simulated. check output + delivery.
fail -> fix by code, not manual patch; rerun.

s8 final lint -- `steps.sh final-lint <skill-dir>`: `routines/skill-layer.py
lint --skill` (layer set, agrees with scope + registration) + real-data shape
scan (e-mail, phone, home path). exit 1 -> fix, rerun from the owning step.
scan is a floor: real names / real case content = your review (rules: body
conventions).

s9 report + note -- report only after s8 passes. one line in session /
worklog: "skill created: <name> -- <purpose>". nightly consolidate harvests
it from conversation context. leave memories/ to the engine.

## flow -- update existing skill

| id | step | lane | after |
|----|------|------|-------|
| u1 | read whole file + map requirement to owning section | llm | - |
| u2 | new ruling replaces old, same commit | llm | u1 |
| u3 | sweep incoming references | llm | u2 |
| u4 | run checks + one-line self-check | llm | u3 |

u1 -- existing home -> edit its sentence. new block only for a topic with no
home.
u2 -- new ruling replaces the old instruction on the same topic in the same
commit.
u3 -- moved instruction -> repair every incoming reference in the same edit:
step ids, section names, relative pointers ("above").
u4 -- rerun s8 + any check the edit touches. self-check line: same instruction
in two places? line count grew -> what earns it?

## rules

### routing (after s1 answer)
Classes:
- (a) framework skill -- generic, product canonical, dogany-* namespace. ships
  with the framework.
- (b) common skill -- shared across this estate's agents, NOT product material.
- (c) personal/local skill -- this instance only.

estate WITH a framework-integration agent (e.g. dev agent):
  framework skill  -> route via dev agent (canonical edit + release + propagation).
  common skill     -> MAIN agent authors. sub/domain agents request main agent
                      instead of authoring themselves. distribution = mechanical
                      copy/install, not re-authoring.
  personal/local   -> author yourself in your own instance.

estate WITHOUT a framework-integration agent (normal product user):
  framework skill  -> keep the shipped skill intact; no hand-edit. options:
                      OVERLAY (author a separate skill that layers desired behavior
                      on top), or disable framework skill from loading and author
                      a separate replacement. (first-class disable mechanism not
                      shipped yet; overlay is the safe path until then.)
  common skill     -> MAIN agent authors (same as above).
  personal/local   -> author yourself.

Hard rule: product canonical repo carries framework skills only. common and
personal skills stay out of it.

### when
- user says: create or update a skill.
- same procedure done 2+ times -> skillify.
- new cron / routine -> pull procedure into a skill.
- multi-step workflow just finished (user-asked) -> offer "make this a skill?".
  propose only, build after OK.

### memory vs skill
- memory (MEMORY.md): facts / prefs / decisions. short, always-injected. = the
  trigger (when / why).
- skill: procedure (how). multi-step, scripts / templates. load on demand -> save
  context.
- trigger -> memory. execution -> skill. keep them apart.

### language + tokens
- SKILL.md body = caveman English. meaning over grammar. drop articles + filler.
  min tokens. agent-read text (steps, rules) = caveman.
- fuller sentences ONLY where meaning goes ambiguous, or in examples.
- user language ONLY where needed: trigger phrases, field / column names, example
  utterances, user-facing output strings.

### body conventions
- examples in rule/step text = schematic placeholders ONLY. show the SHAPE of
  the rule, not a real case. use tokens like `<USER_NAME>`, `<PROJECT_X>`,
  `<DATE>`, generic labels (e.g. "item A", "category Y").
- keep actual user/agent observed data out of rule text (real names, real
  content, real event pairs). reason: bakes a live-instance bias into the
  rule and leaks personal data into shipped skill text.
- skill body owns current instruction; tests own real cases; ticket owns
  reasons + history. incident narrative -> one ticket-ID pointer (DGN-1373).
- each block names its reader: agent / maintainer / product user; reader
  split is the cognition-cost ledger.

### location + description
- path: ~/<workspace>/.claude/skills/<name>/SKILL.md; aux files same folder.
- description = match text, loaded every session = auto-trigger core. weak desc ->
  agent hand-codes instead of calling skill. make self-sufficient:
  1. list real trigger utterances (user language).
  2. cover ALL cases: log / run AND query / status / edit. (miss query -> skill
     wont fire on that phrase.)
  3. positive phrasing ("handles X").
  4. state outputs / side-effects. example: "final output = status card via
     send_file".
  check: write 3-5 sample utterances, confirm desc keywords catch them.

### trigger tiers (best-effort vs guaranteed)
- BEST-EFFORT = description auto-fire. model reads desc, judges, MAY miss (weak
  desc, odd phrasing, cross-lingual). fine for helpful/optional behavior.
- GUARANTEED = must-happen step (data integrity, always-send card, always-confirm).
  desc alone NOT enough -> model can skip it. wire a hook (PostToolUse /
  SessionStart / etc) for it.
- any skill with a must-happen step declares its tier in SKILL.md, and if
  GUARANTEED, wires the hook. example: card-followup PostToolUse hook forces the
  card render after a log -> that is the guarantee, desc is only the nudge.

### lane partition (rule vs llm) -- per step, before writing prose
- each enforced rule has one owner. code-owned rule -> prose names its
  hook / script / lint; owner keeps gate + prose aligned in the same edit.
- one step = one output kind. step yields value AND claim -> split first.
- test, in order:
  1. same input -> same required output? no -> llm lane. yes -> next.
  2. input machine-readable at that point? no -> SPLIT (llm parses ->
     rule computes), retest parts. yes -> rule lane.
  3. rule lane -> carrier MUST exist (script / query / hook) inside the
     skill folder. no carrier -> build it or step does not ship. formula /
     lookup order / date stamp / dedup living in prose = defect, not style.
- rule output = value | gate | verify ONLY. world-claim ("fast day", "no brand
  product") -> llm lane, with provenance fields (estimated flag, closed flag,
  counts) in hand.
- ambiguous after split -> llm lane + verify step. confident-wrong rule
  beats slow judgment for damage. dont build it.
- declare in skill product.yaml `lanes:` block (contract / step id / lane /
  kind / carrier / after). lane-lint (the canonical repo's
  scripts/lane-lint.py, wired at the publish gate) checks carrier exists,
  kind is value|gate|verify, refs resolve. lint does NOT judge the lane
  call -- that stays design review. (publish gate: framework-class skill
  authoring only, canonical repo; local/shared skills never reach it.)
- smell: prose un-saying a rule output ("never voice X as Y") = rule
  claims too much. shrink rule to value/gate, give residue to llm.
- doctrine: PRODUCT-DOCTRINE D10.

### driver-mode hook pattern (skills that drive a CLI subagent)
Case: skill shells out to a CLI subagent (e.g. `claude --agent X`) AND the
human may also drive that subagent directly. Skill-owned session registry
breaks: manual run = second writer -> registry drifts (one-writer violation).

Fix: registry ownership -> Claude Code hooks in the target agent's project
`.claude/settings.json`. Hooks fire on EVERY invocation of that agent
(skill-driven or manual) while cwd is the project dir -> hook = single writer.

Wiring:
- SessionStart hook: write/refresh registry entry (session_id + source;
  source = "startup" for new, "resume" for --resume).
- Stop hook (fires per turn): increment turn count; crossing threshold flags
  rotation for next SessionStart.
- filter by agent_type in payload: plain `claude` session in same dir leaves
  registry alone.
- concurrency guard: hook writes per-project liveness marker (pid + timestamp);
  second session starting on same project while one is live -> warning;
  sequential use stays silent.
- skill only READS registry (resume vs new decision); hook owns all writes.

Caveats:
- hook payload field names (session_id, cwd, agent_type, source,
  hook_event_name, prompt_id, last_assistant_message) are a Claude Code
  implementation detail, NOT a stable public contract. probe in-repo before
  wiring.
- hooks MUST be fail-open: on any parse error, exit 0. session always runs.

Pattern verified live across Dogany instances.

### tone
- message skills -> follow shared output rules (RULES output/notation +
  identity/hot.custom.agent.md output discipline). point to them; dont restate.

### model routing (cost)
- routine / high-freq / simple -> haiku
- data wrangle (summarize, aggregate) -> sonnet
- conversation / hard reasoning -> opus

### size review
- skill feels big -> editor audits each block in 4 columns: instruction /
  WHY (ticket ID or "unattributed") / class (live, duplicate with owning
  gate, scar surviving its fix, unclassified) / reader; then consolidates.

### bounds
- size judged by what breaks: verbatim approval copy IS the product; live model
  speech belongs to the model; hard-to-reverse branches with quiet failure
  modes earn detail. these surviving instructions own their line cost.
- gateway / main bot restart or stop -> ask user; skill leaves them running.
- external action (mail, public post) -> add pre-send user-confirm step.
