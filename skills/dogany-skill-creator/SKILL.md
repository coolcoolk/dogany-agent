---
name: dogany-skill-creator
display_name: 스킬 만들기
description: Meta-skill. Make new skills THIS system's way. Triggers: user says make / skillify a skill; agent packages a repeated procedure (cron / routine / workflow). Read before ANY skill / cron / routine. Enforces naming, path, caveman-English body, model routing, tests.
---

# skill-creator -- skill-making standard

Stop drift. Read before ANY skill / cron / routine. THIS system's rules, not
generic. Self-obeys (caveman body).

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
