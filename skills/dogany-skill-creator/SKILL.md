---
name: dogany-skill-creator
display_name: 스킬 만들기
description: Meta-skill. Make new skills THIS system's way. Triggers: user says make / skillify a skill; agent packages a repeated procedure (cron / routine / workflow). Read before ANY skill / cron / routine. Enforces naming, path, caveman-English body, model routing, tests.
---

# skill-creator -- skill-making standard

Stop drift. Read before ANY skill / cron / routine. THIS system's rules, not
generic. Self-obeys (caveman body).

## authoring routing gate (read FIRST)

Before authoring ANY skill, classify it, then route.

Classes:
- (a) framework skill -- generic, product canonical, dogany-* namespace. ships
  with the framework.
- (b) common skill -- shared across this estate's agents, NOT product material.
- (c) personal/local skill -- this instance only.

Route by class:

estate WITH a framework-integration agent (e.g. dev agent):
  framework skill  -> route via dev agent (canonical edit + release + propagation).
  common skill     -> MAIN agent authors. sub/domain agents request main agent
                      instead of authoring themselves. distribution = mechanical
                      copy/install, not re-authoring.
  personal/local   -> author yourself in your own instance.

estate WITHOUT a framework-integration agent (normal product user):
  framework skill  -> NEVER destroy or hand-edit the shipped skill. options:
                      OVERLAY (author a separate skill that layers desired behavior
                      on top), or disable framework skill from loading and author
                      a separate replacement. (first-class disable mechanism not
                      shipped yet; overlay is the safe path until then.)
  common skill     -> MAIN agent authors (same as above).
  personal/local   -> author yourself.

Hard rule: non-framework skills (common or personal) must NEVER be pushed into
the product canonical repo. framework canonical carries framework skills only.

## when
- user says: make a skill.
- same procedure done 2+ times -> skillify.
- new cron / routine -> pull procedure into a skill.
- multi-step workflow just finished (user-asked) -> offer "make this a skill?".
  propose only, build after OK.

## memory vs skill
- memory (MEMORY.md): facts / prefs / decisions. short, always-injected. = the
  trigger (when / why).
- skill: procedure (how). multi-step, scripts / templates. load on demand -> save
  context.
- trigger -> memory. execution -> skill. never mix.

## language + tokens (default)
- SKILL.md body = caveman English. meaning over grammar. drop articles + filler.
  min tokens.
- fuller sentences ONLY where meaning goes ambiguous, or in examples.
- user language ONLY where needed: trigger phrases, field / column names, example
  utterances, user-facing output strings.
- agent-read text (steps, rules) = caveman.

## location + structure
- path: ~/<workspace>/.claude/skills/<name>/SKILL.md
- name: kebab-case, verb-ish.
- aux files (templates, scripts) -> same folder.
- frontmatter: name + display_name + description. display_name = short user-facing
  label (Korean for Korean-persona instances; user's language otherwise). never
  speak the folder ID to the user -- use display_name instead.
- description = match text, loaded every session = auto-trigger core. weak desc ->
  agent hand-codes instead of calling skill. make self-sufficient:
  1. list real trigger utterances (user language).
  2. cover ALL cases: log / run AND query / status / edit. (miss query -> skill
     wont fire on that phrase.)
  3. positive phrasing ("handles X"), not negative.
  4. state outputs / side-effects. example: "final output = status card via
     send_file".
  check: write 3-5 sample utterances, confirm desc keywords catch them.

## trigger tiers (best-effort vs guaranteed)
- two tiers. dont confuse them.
- BEST-EFFORT = description auto-fire. model reads desc, judges, MAY miss (weak
  desc, odd phrasing, cross-lingual). fine for helpful/optional behavior.
- GUARANTEED = must-happen step (data integrity, always-send card, always-confirm).
  desc alone NOT enough -> model can skip it. enforce with a hook (PostToolUse /
  SessionStart / etc), not description.
- rule: any skill with a must-happen step declares its tier in SKILL.md, and if
  GUARANTEED, wires the hook. example: card-followup PostToolUse hook forces the
  card render after a log -> that is the guarantee, desc is only the nudge.

## driver-mode hook pattern (skills that drive a CLI subagent)

When a skill shells out to a CLI subagent (e.g. `claude --agent X`) AND the
human may also drive that same subagent directly outside the skill: do NOT let
the skill own the session registry. Manual run = second writer -> registry
drifts. One-writer-invariant violation.

Fix: move registry ownership to Claude Code hooks wired into the target
agent's project `.claude/settings.json`. Hooks fire on EVERY invocation of
that agent (skill-driven or manual) as long as cwd is the project dir -> hook
is the single writer regardless of who drives.

Wiring:
- SessionStart hook: write/refresh registry entry (session_id + source;
  source = "startup" for new, "resume" for --resume).
- Stop hook (fires per turn): increment turn count; crossing threshold flags
  rotation for next SessionStart.
- Filter by agent_type in payload: plain `claude` session in same dir must not
  touch registry.
- Concurrency guard: hook writes per-project liveness marker (pid + timestamp);
  second session starting on same project while one is live -> warning;
  sequential use stays silent.
- Skill only READS registry (resume vs new decision); hook owns all writes.

CAVEATS:
- Hook payload field names (session_id, cwd, agent_type, source,
  hook_event_name, prompt_id, last_assistant_message) are a Claude Code
  implementation detail, NOT a stable public contract. Probe in-repo before
  wiring.
- Hooks MUST be fail-open: on any parse error, exit 0 -- never block the
  session.

Pattern verified live across Dogany instances.

## tone
- message skills -> follow shared output rules (RULES output/notation + AGENT.md
  output discipline). dont restate.

## model routing (cost)
- routine / high-freq / simple -> haiku
- data wrangle (summarize, aggregate) -> sonnet
- conversation / hard reasoning -> opus

## build order (strict)
1. decide: memory or skill.
2. write folder + SKILL.md. keyword-rich desc. pick display_name (2-4 word
   noun phrase; Korean for Korean-persona instances). add as frontmatter line
   right after `name:`. never speak the folder ID to the user.
3. write aux scripts / templates.
4. real run test (not simulated). check output + delivery.
5. report only after pass. fix by code, not manual patch.
6. leave one-line note in session/worklog: "skill created: <name> -- <purpose>". nightly consolidate harvests from conversation context. do NOT hand-write memories/.

## bounds
- never restart / stop gateway or main bot from a skill -> ask user.
- external action (mail, public post) -> add pre-send user-confirm step.
