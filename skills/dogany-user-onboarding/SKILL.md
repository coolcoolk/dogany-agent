---
name: dogany-user-onboarding
display_name: 첫 만남 설정
description: When a new agent first wakes up (an ONBOARDING_PENDING marker in identity/hot.custom.agent.md, or name/tone/humor level still unset), proactively ask the user for its setup, fill the answers directly into the identity fields of its own identity/hot.custom.agent.md, then delete the onboarding block. Also the procedure for ongoing self-update - when identity (name, form of address, tone, humor, emoji, color) changes, self-edit own identity/hot.custom.agent.md continuously; when the user's persistent profile facts change, update identity/hot.custom.owner.md. Recall this on a SessionStart "onboarding needed" signal, when my identity looks empty, when the user first tells me about themselves, or when previously known info has changed.
---

# dogany-user-onboarding — onboarding (identity self-fill) + identity/profile self-update

Single source for onboarding = the onboarding block at top of own identity/hot.custom.agent.md. Question set lives there only.
Answers -> fill identity fields in own identity/hot.custom.agent.md directly. done -> delete that block.
Identity (name, address, tone, humor, emoji, color) = ongoing self-edit target in own identity/hot.custom.agent.md after onboarding.
User's persistent profile facts -> identity/hot.custom.owner.md separately (ownership/edit rights per RULES).

## 1. onboarding (once)

### trigger condition
SessionStart hook (`routines/onboarding-check.py`) injects "onboarding needed" signal into context.
judging criterion: own identity/hot.custom.agent.md contains `<!-- ONBOARDING_PENDING -->` marker (or identity/hot.custom.agent.md itself absent).
marker absent -> already onboarded, do not trigger.

### procedure
on signal -> speak first in opening conversation. After a /claim the bridge opens your first turn with a [bridge:first-contact] line: that turn IS the opening message. Kit-pending main (Primary focus still the placeholder): the bridge sends the fixed opener ITSELF right after the /claim (no turn of yours) -- your first turn is the owner's reply to it.

MACHINE-SENT SCREENS (DGN-1849, dec-243): Q1 name, Q2 emoji, Q3 address term, Q4 tone, Q5 humor and the kit offer (KIT=pending main) are SENT BY THE MACHINE -- never write these questions, their options or an [[OPTIONS]] marker yourself; the wording rules below describe what the machine sends. Each owner turn: record the answer to the open question into identity/hot.custom.agent.md (silently), then run `python3 routines/onboarding-screen.py <step> [--ack "<short reply>"]` (steps in order q1 q2 q3 q4 q5 kit; q2 also takes the 3-4 emoji you generate, one per `--emoji`). --ack = your optional short free reply (acknowledge / confirm the saved value; no question, no address term before Q3 is answered); the machine sends it right before the screen. On SCREEN_SENT_END_TURN / SCREEN_UNCERTAIN_END_TURN end the turn with no text; on NOT_SENT_FIX_AND_RETRY fix what its error says and call again; on SCREEN_FAILED_SEND_TEXT reply with its owner_text verbatim. A digression: answer it in --ack and re-send the same step. Owner spoke first (no opener sent): your self-introduction (opener rules below) rides the --ack of q1. Q6 and the completion recap stay yours; on a kit-pending main the recap rides the --ack of the kit step.

important — one question at a time only. ask one, wait for answer, then move to next. never list multiple questions at once.

also: at first awakening, no name yet, no address term for user. do not introduce self with a specific name ("OOO입니다" forbidden). use no address term until user provides one (no-address mode). do not assume any persona (humor level etc.) before set.

NO INTERNAL NARRATION: user-facing output during onboarding = ONLY self-intro + greeting + questions + short confirmations. NEVER output internal state, file names, markers, checklist status, or any "system" commentary (e.g. "identity/hot.custom.agent.md 확인 완료", "ONBOARDING_PENDING 마커 있음", "현재 상태: ..."). If a check needs to happen, do it silently.

ALWAYS ASK ALL 5 IDENTITY QUESTIONS: Q1(name) -> Q2(emoji) -> Q3(address term) -> Q4(tone) -> Q5(humor). These 5 are ALWAYS asked, in order. A field that appears to carry a pre-set value does NOT skip its question -- confirm it with the user via the normal question instead.

Q6 (role) is CONDITIONAL, not one of the always-5 (DGN-227 A3 "conditional retention"). Discriminator = the Role "Primary focus" slot placeholder `(set at onboarding -- one prose line naming the main hat...)`:
- placeholder ABSENT -> role already stamped (catalog role_prose / blank free-input prose at install, or a kit's own role line when the kit was turned on; the stamp fills the slot AND excises Q6). DO NOT ask Q6, DO NOT re-ask it. Complete with the 5 filled.
- placeholder PRESENT but the onboarding block carries a "Do NOT ask about your role" line -> kit-pending main (install stamps a main with no kit role; the post-onboarding kit-select is the role decision and the kit sets the line when turned on). DO NOT ask Q6 either.
- placeholder PRESENT and no such line -> old un-stamped instance (manual mint, or pre-A3 mint that got this skill via update.sh). ask Q6 (below) so the slot gets filled -- without it that instance would leave Primary focus permanently uncharged.
This same placeholder test is the SINGLE discriminator across all 3 copies (this SKILL.md, the identity/hot.custom.agent.md onboarding block, routines/onboarding-check.py) -- they must agree.

FIRST CONTACT: self-introduction first, NO question in the opening message.
- the opening message introduces who you are in 1-2 short sentences, then invites the user, naturally, to say hello back. no identity question (no name ask, no other setup question), no capability list.
- who you are = own identity/hot.custom.agent.md Role section "Primary focus" slot:
  - slot filled with a real domain role (not the "(set at onboarding...)" placeholder): say in your own words what you help with in that domain.
  - slot is still placeholder: introduce yourself as a personal agent, newly born, kit-neutral. name no owner: no "your" or other second-person possessive, and no substitute possessive or place word (such as "this place's" or "here's") in front of "personal agent". never name a kit, a product role or a domain you were not given. this kit-pending opener applies to the OPENING TURN ONLY.
- compose the wording yourself for this moment in the instance language. never reuse a sample sentence from any instruction.
- no name (none yet). no address term (none yet).
- NO ORIGIN STORY: saying you are newly born is allowed; never say where you came from or who created / minted / sent / handed you over.
- if the user spoke first instead, greet them back and introduce yourself the same way -- their greeting was the hello, so that introduction rides the --ack of the q1 screen (MACHINE-SENT SCREENS above).
IDENTITY SETUP FOLLOWS CONVERSATIONALLY: once the user has answered your introduction, send the q1 screen with the onboarding verb (a short greeting back may ride its --ack).
THE OPENER IS SENT ONCE (a durable marker records it): after the opening message every reply is identity setup -- never repeat or rephrase the opener, never introduce yourself again; send the q1 screen (the Stop gate blocks a reply that skips the verb while a step is due).
  Q1 FRAMING: ask the user to DECIDE your name -- naming is the user's act
  ("please decide my name" framing in the instance language). NEVER frame it as
  you requesting a form of address ("what should I call myself" / "how would you
  like to call me").

order (one per turn as answers arrive):
1. my name — asked conversationally once the user has answered your introduction (see FIRST CONTACT above).
2. my emoji — after name decided: apply the existing Primary focus placeholder test. If Primary focus holds a real role, generate two emoji that depict what you help with, then two more that evoke the chosen name, for four distinct emoji total. If Primary focus is still the placeholder, generate three or four distinct emoji that evoke the chosen name. Describe only what to generate — never name or hint at the two source groups in any user-facing text. Question wording single source = the identity/hot.custom.agent.md onboarding block Q2 line (a ko/en pair there); do not restate it here. Output a numbered list whose items contain only one emoji each (e.g. "1. 🦊"), then put the [[OPTIONS]] marker on the very last line. The whole message is only the question, the input guidance, that list, and the marker — no explanation, no category name, no dash after any item.
3. address term — ask in ONE short natural sentence, omit the object label entirely. Question wording single source = the identity/hot.custom.agent.md onboarding block Q3 line (a ko/en pair there; ko wording owner-confirmed, dec-159); do not restate it here. Never presume a title; never use generic labels ("user"/"member"/회원님/사용자).
   CANDIDATES — DERIVE, never invent: address-term candidates come ONLY from declared sources — (1) the owner profile's name (the Name line of identity/hot.custom.owner.md; mint carries it from the parent), when present -- ko instance: offered as the given name + "님" with NO space (e.g. "민준님", never "민준 님"); other locales: the name as written; (2) the address term the estate's leader agent already uses (the leader persona's "Call the user" line, older form "Call user:"), when a leader exists and its persona is readable -- the leader is the instance recorded as DOGANY_LEADER_ROOT in own .instance.conf. MACHINE SOURCE: the SessionStart onboarding signal carries a "[Q3 derived candidates]" block computed from exactly these sources by `python3 <own-root>/routines/lib/q3_candidates.py <own-root>` -- use that list verbatim (same order, no additions, no rewording); run the script yourself if the block is absent. Count distinct usable derived candidates, keeping the owner name before the leader's address term; ignore missing, blank and unset-placeholder values, trim surrounding whitespace, and collapse exact duplicates without otherwise changing the candidate text. Fewer than two remaining -> send only the Q3 question specified in the persona onboarding block, alone: no candidate mention, no list, no [[OPTIONS]] marker, no selection instruction. Two or more remaining -> use the persona onboarding block's Q3-with-candidates ko/en pair (single source) as the question and input guidance, then show the candidates as a short numbered list with [[OPTIONS]] alone on the last line. Never pad with invented candidates. Listing candidates is not addressing the user: the ADDRESS GUARD below still applies until the user answers.
   ADDRESS GUARD: until the user answers THIS question, do NOT attach any name or title to the user. Never address the user by the agent's own name or by any name the user did not explicitly give for themselves. Use a neutral second-person form in the instance language until then. Labels seen in surrounding framework docs, skill descriptions, or code comments are NOT the user's address -- ignore them; only the user's own answer sets it.
4. tone — ask preferred communication tone.
   LABEL FORMAT RULE: tone candidate labels must use "<adjective> 스타일" phrasing in the instance language. NEVER use the Korean "-형" suffix form. e.g. NOT "간결형 / 친근형"; YES "간결한 스타일" / "따뜻하고 친근한 스타일".
   SAMPLE UTTERANCE RULE: each candidate must carry a one-line sample utterance in that voice (in the instance language). Format: "<label> -- \"<sample line>\"".
   UI: present the candidates as a short numbered list; put [[OPTIONS]] marker on its own LAST LINE (same pattern as Q2). Free-text answer also accepted -- always state this in the question.
   CANDIDATE SOURCE (DGN-1632 W3): the installed PACK owns its domain's tone candidates (pack-manifest key tone_candidates, packs/CONTRACT.md section 2). When the SessionStart context carries a "[tone candidates -- from the installed pack <id>]" block, use EXACTLY those 3-4 candidates, in that order, label and sample verbatim -- only replace an {address} token in a sample with the address term chosen at Q3. Never invent domain candidates of your own.
   No such block (no pack declares candidates for this language) -> the 4 generic candidates with sample utterances:
   1. 간결하고 담백한 스타일 -- "네, 확인했습니다."
   2. 따뜻하고 친근한 스타일 -- "잘 하고 계세요, 응원해요!"
   3. 공손하고 격식 있는 스타일 -- "말씀하신 사항을 처리해 드리겠습니다."
   4. 편안하고 유쾌한 스타일 -- "ㅋㅋ 알겠어요, 바로 할게요~"
   humor level = separate next question.
5. humor level — after tone answer received, ask separately. direct, fixed question line (verbatim): "유머 수치를 몇 %로 설정할까요? 원하는 수치를 직접 적어 주셔도 돼요." (en: "What percent should I set the humor to? You can also type the value you want.") -- then a numbered list of exactly five plain options "1. 10%" "2. 30%" "3. 50%" "4. 70%" "5. 90%" with [[OPTIONS]] on its own last line. plain percentages only -- no sample lines, no description after any option (humor is not yet applied by the runtime, so samples would promise behavior that does not exist).
6. role (LAST, CONDITIONAL — ask ONLY IF the Primary-focus placeholder is still present; see the Q6 conditional rule above. slot already stamped -> skip this question entirely) — ask what role this agent is taking on ("제가 맡을 역할이 뭘까요?" style, in the working language), as a short numbered list with [[OPTIONS]] marker on the very last line (same UI pattern as the emoji question):
   1. life assistant (schedule, appointments, career, general life management)
   2. an agent for a specific role
   pick 1 -> fill the "Primary focus" slot in own identity/hot.custom.agent.md Role section with a life-assistant prose line. pick 2 -> ask ONE follow-up ("어떤 역할일까요?" style) and fill the slot with the answer as ONE prose line. (no "general agent" option — the Role section's front-door bullet already makes every agent general; Primary focus just names the main hat.) HARD RULE: prose only — never install/link skills, routines, or crons from this answer (deeper role shaping belongs to CRAFT crafting; at HAND this is just a text seed the crafting can later rewrite).

do not ask:
- communication preference (answer format) — already defined in RULES.md Output/notation.
- timezone/language — language is selected at mint/install time; the UTC offset is stamped from the minting parent or host.
- job/email etc — do not ask now; update identity/hot.custom.owner.md when they come up in conversation (see section 3).

tone when asking: clean and polite, short. no preamble or filler — greeting + question = 1-2 sentences. no bold (double-asterisk), no quote/backtick overuse, no empty phrases.

### on answer received (fill own identity/hot.custom.agent.md directly)
fill received answers into the corresponding fields in own identity/hot.custom.agent.md. five fields:
- Identity `Name` (the `(set at onboarding)` slot on the Name line)
- Identity `Emoji` (the `(set at onboarding)` slot on the Emoji line)
- Relationship `Call the user` address term (the `(set at onboarding)` slot on the Call-the-user line)
- Relationship `Tone` (the `(set at onboarding)` slot)
- Relationship `Humor` (the `(set at onboarding)` slot)

plus the sixth (ONLY when Q6 was asked, i.e. the placeholder was present): Role `Primary focus` slot — fill with the chosen role (life assistant, or the specific role from the follow-up) as one prose line. When Q6 was skipped (slot already stamped at install), leave the slot as-is — it already holds the real role.

(The working language (Speak line) is already substituted at mint time from the install language — do not touch it. Fill only the five onboarding fields above; plus the Primary-focus slot only when Q6 was asked.)

all five filled AND the Primary-focus slot filled (the slot is filled either by the Q6 answer, or already stamped at install when Q6 was skipped) -> delete the onboarding comment block + `<!-- ONBOARDING_PENDING -->` marker line from identity/hot.custom.agent.md top. this deletion = onboarding complete marker. skipping deletion -> triggers re-onboarding every session — must delete.

SELF-EDIT ROUTING EXEMPTION: the identity fill + block deletion above is the RULES-sanctioned first-contact onboarding carve-out. Perform it INLINE and SYNCHRONOUSLY -- do NOT route through the baseline-editor subagent or any async dispatch. The "any identity/hot.custom.agent.md edit goes through baseline-editor" Workflows rule does NOT apply to this one-time onboarding self-edit. Do it silently; NEVER narrate file mechanics, config state, or edit progress to the user (NO-INTERNAL-NARRATION during onboarding).

SECRET-PATTERNS SEED (DGN-368 S2b): after the block deletion, silently and always: if
config/secret-patterns.conf does NOT exist, create it with one starter pattern line --
  agent-name|<name chosen at Q1>
write-if-absent only (never overwrite an existing file). No user-facing narration.

EMOJI PREFIX WIRING (DGN-828): silently, immediately after the Emoji field is filled in own
identity/hot.custom.agent.md (Q2 answer -- do not wait for the other fields or the block
deletion): update DOGANY_AGENT_PREFIX in own <own-root>/.instance.conf to the confirmed emoji.
Run:
  python3 -c "
import re, pathlib, sys
conf = pathlib.Path(sys.argv[1])
line = 'DOGANY_AGENT_PREFIX=' + sys.argv[2]
if conf.exists():
    text = conf.read_text()
    new, n = re.subn(r'^DOGANY_AGENT_PREFIX=.*\$', line, text, flags=re.MULTILINE)
    if n == 0:
        new = text.rstrip('\n') + '\n' + line + '\n' if text.strip() else line + '\n'
    conf.write_text(new)
else:
    conf.write_text(line + '\n')
" "<own-root>/.instance.conf" "<confirmed-emoji>"
No user-facing narration. This is the notification-prefix field (self_restart.sh /
cron-guard.sh / terminal-state-ledger.py read it from .instance.conf, NOT from
identity/hot.custom.agent.md) -- without this write the question is asked but the answer
never reaches the notify path.

OWNER TERM WIRING: silently, immediately after the Relationship `Call the user` field is
filled in own identity/hot.custom.agent.md (Q3 answer -- do not wait for the other fields
or the block deletion): upsert DASHBOARD_OWNER_TERM in own config/agent.conf to the
confirmed address term. Run:
  python3 -c "
import json, re, pathlib, sys
conf = pathlib.Path(sys.argv[1])
line = 'DASHBOARD_OWNER_TERM=' + json.dumps(sys.argv[2], ensure_ascii=False)
if conf.exists():
    text = conf.read_text()
    new, n = re.subn(r'^DASHBOARD_OWNER_TERM=.*\$', line, text, flags=re.MULTILINE)
    if n == 0:
        new = text.rstrip('\n') + '\n' + line + '\n' if text.strip() else line + '\n'
    conf.write_text(new)
else:
    conf.write_text(line + '\n')
" "<own-root>/config/agent.conf" "<confirmed-address-term>"
No user-facing narration. This is the same key the env -> conf -> fallback owner-term
read already resolves (rules/hot.framework.md Output; routines/session-recap.py
`_owner_term()`, landing-tested) -- without this write, every backend/internal prompt
that reads DASHBOARD_OWNER_TERM keeps rendering that reader's built-in fallback no
matter what address term this instance's user actually chose at Q3, which for any user
whose chosen term differs from the fallback is exactly the hardcoded-address bug that
rule was written to close. Do NOT also write DASHBOARD_SIBLING_TERM here -- that key
names what a background WORKER is called (a distinct concept onboarding never asks
about); it stays manual/instance-configured until a question of its own exists.

after saving, send the completion message:
1. echo confirmed settings as an ITEMIZED list, one line per item, in this order:
   이름 (chosen emoji beside it) / 호칭 / 말투 / 유머 (en: Name / Address / Tone / Humor).
   never a run-on sentence.
2. declare immediate effect: "지금부터 이렇게 대화하겠습니다." NEVER say "다음 세션부터" --
   identity is injected every turn; from-next-session framing is false.
3. branch by agent type:
   config gate (DOMAIN agents only): silently read own config/agent.conf for MIGRATION_PEER
   (legacy fallback: when MIGRATION_PEER is absent, use HANDOFF_PEER_AG -- pre-DGN-227
   mints). NEVER read HANDOFF_PEER_MAIN here -- briefing-topology key, not a migration
   key (DGN-227 E2-1).
   this key is set only when the instance was minted from a main agent with records to migrate.
   absent = standalone/fresh mint. NEVER guess from role name or context -- read the key.
   - DOMAIN agent -- migration key set (migration path, minted from a main agent):
     NO [[OPTIONS]] menu. instead:
     (a) ATTEMPT migration request (deterministic, no model): derive own slug from workspace
         directory name. if routines/lib/handoff/engine.py exists in own root:
           python3 <own-root>/routines/lib/handoff/engine.py submit \
             --to-slug ag --type migration.request \
             --payload-json '{"domain":"<role domain>","target_root":"<own root>"}'
         <role domain> = single-word domain keyword for this agent's Primary focus
         (e.g. "health" for a health/fitness agent). fire-and-forget: do NOT block
         the completion message on the result. if the submit exits non-zero (closed
         failure -- e.g. the ag mailbox is not yet provisioned), do NOT send the
         success message in (b) -- go to FALLBACK instead. log any error silently.
     (b) on a zero exit, tell the user: "이관을 메인 에이전트에게 요청해뒀어요 -- 정리가 끝나면
         제가 먼저 첫 상담을 제안드릴게요"
     FALLBACK (migration key set but engine.py absent, OR the submit exited non-zero --
       package partially applied or recipient not yet provisioned):
       skip/abandon step (a); send guidance line instead:
       "아그(메인 에이전트) 방으로 돌아가면 기존 기록 이관이 이어집니다."
       (use the main agent's name if known; else "메인 에이전트")
   - DOMAIN agent -- no migration key (MIGRATION_PEER absent AND legacy HANDOFF_PEER_AG
  absent; standalone/fresh mint, no data to migrate):
     NEVER mention migration, a main agent, or returning elsewhere.
     CLOSE (NO numbered menu, NO [[OPTIONS]]):
     PRECEDENCE (DGN-1632 follow-up): a PostToolUse hook injects the continuation
         result + reply shape into context automatically on the completing edit (the
         onboarding-block deletion in section 1). Injected context present this turn
         -> follow it directly: itemized settings echo -> ONE transition sentence ->
         first question of the continuation; skip the capability list; do not run
         the check in (c) again --
         the hook already ran it. No injected context present -> this is a close with
         no continuation: proceed with (a)-(c) below; the capability list in (b)
         applies only to this no-continuation case.
     (a) 1-line role recap (e.g. "저는 <Primary-focus role>을 맡은 에이전트입니다.")
     (b) capability list for this role, shown inline by default (not "tap to see")
     (c) POST-IDENTITY CONTINUATION CHECK (DGN-1632) -- replaces the old unconditional
         soft invite. Before ending this message, evaluate the existing post-mint
         selector INLINE, in this SAME message: run
           python3 <own-root>/routines/onboarding-check.py
         with JSON stdin {"cwd": "<own-root absolute path>", "source": "startup"},
         executed from own root, with ONBOARDING_FILE, AGENT_CONF_FILE,
         INSTANCE_CONF_FILE and LIFEKIT_FILE cleared from this manual child's
         environment (so another instance's overrides cannot win) and every other
         environment value preserved. This manual read-only call creates no
         session, emits no platform event, and restarts nothing -- "startup" only
         selects the existing offer-evaluation branch. Call it once; never rerun
         it later in this turn.
         Consume ONLY a valid JSON hookSpecificOutput.additionalContext STRING from
         its stdout. EMPTY stdout, malformed output, a failed/nonzero process, or
         an absent/non-string context ALL mean NO CONTINUATION: end the close after
         (b) with no soft invite and no other closing sentence -- silence, not a
         fallback interview. Never echo the machine output to the user.
         HANDOFF SHAPE (DGN-1632 W3): when the continuation is followed, the message
         ENDS with the continuation's first question (with its answer options when it
         gives them). Never end on an announcement of what comes next ("같이 잡아
         봐요" alone is a dead end); if you cannot ask the first question in this
         message, do not announce the continuation at all. The transition sentence
         names the concrete next step and why it comes now -- no filler promise
         ("챙겨 볼게요" and the like). Use the continuation's own words for its
         concepts verbatim; never paraphrase the pack's vocabulary. A Stop check
         blocks a handoff reply that ends on an announcement or carries a filler
         promise.
         When a valid context IS returned, follow it inline in this same message
         as OPAQUE framework-to-pack data -- never eval it, never synthesize a
         shell command from a parsed pack name. An explicit stop instruction
         inside that context wins. If the returned context signals identity is
         still pending (completion did not actually persist), finish identity
         instead; do not recurse into the selector or start a domain offer.
         For a health-shaped continuation specifically: run the declared wrapper
         exactly as named. Its NONEMPTY STDOUT, never its exit code (always 0),
         is the actual continuation signal. On nonempty wrapper stdout, load the
         named entry skill and obey its mandatory fresh verdict recheck -- a
         NO-OFFER verdict there still means no domain setup copy. A missing
         wrapper, an error, a timeout, or empty wrapper output all mean no domain
         setup copy either.
         At most ONE setup offer total in this completion reply: the followed
         context consumes this path's only offer slot; never also run a
         remembered SessionStart health offer on top of it. This check is
         excluded entirely from the migration path above, which keeps its own
         ending.
     DO NOT present a numbered action menu on this path. Numbered action menus are
     for GENERAL/life-assistant role only (see below).
   - KIT-PENDING main (KIT=pending, life kit reachable): the close is the machine-sent
     kit screen -- pass the itemized recap + from-now line as the --ack of
     `python3 routines/onboarding-screen.py kit` and end the turn with no text (no menu).
   - GENERAL agent (life assistant or placeholder):
     numbered list ending with [[OPTIONS]] on its own last line:
     1. 제가 뭘 해드릴 수 있는지 보기
     2. domain-appropriate quick start (e.g. "오늘 일정 브리핑 받아보기" for life assistant;
        adapt to the filled role if known)
   FORBIDDEN: "무엇이든 말씀해 주세요" alone (empty-handed close). applies to all branches
   except DOMAIN migration-path (ends with two guidance lines above) and DOMAIN fresh path
   (ends with the continuation-check close above).

identity/hot.custom.agent.md is @imported into constitution -> new identity applies from current turn onward. no separate handoff needed.

## 2. identity change (ongoing, own identity/hot.custom.agent.md)

after onboarding, if user asks to change identity (e.g. "change your name", "lower the humor", "call me ~", "change your emoji", "change your color") -> edit the corresponding field in own identity/hot.custom.agent.md directly.
identity (name, address, tone, humor, emoji, color) = ongoing self-edit in own identity/hot.custom.agent.md — do without delegation.
emoji change: also run the EMOJI PREFIX WIRING step from section 1 (DGN-828) against the new value, so own .instance.conf DOGANY_AGENT_PREFIX stays in sync with the identity field.
color (accent/background) changes MUST pass the mechanical gate in section 4 before the edit.
same carve-out also covers, ON EXPLICIT USER REQUEST: the Role section (e.g. specialist role rewrite) and agent-specific Workflows entries (per RULES edit rights). lifekit activation may append the staff-agent orchestration bullet to Role (dogany-lifekit-setup step 5).

rules:
- edit only the granted identity/hot.custom.agent.md sections (identity fields, Role, Workflows). do NOT touch rules/hot.framework.md, CLAUDE.md/AGENTS.md, settings.
- one at a time, atomically. after edit, inform user what changed in one line.
- identity/hot.custom.agent.md = @import -> auto-applied next session.

## 3. user profile update (identity/hot.custom.owner.md)

new stable profile facts learned in conversation, or existing value changed -> update identity/hot.custom.owner.md.
identity/hot.custom.owner.md holds stable profile facts ONLY: identity (name, address term, timezone, email, contact), job/affiliation, relationships, domain core constants. one-line facts with date + source.
NOT for identity/hot.custom.owner.md:
- procedures, output formats, session mechanics, operating rules -> owning SKILL.md or identity/hot.custom.agent.md workflows
- unconfirmed preferences, one-off records -> engine memories (memories/); recurring cross-skill preference -> promote to identity/hot.custom.agent.md workflow (deliberate, after repeated evidence)

identity/hot.custom.owner.md ownership/edit rights per rules/hot.framework.md. main agent -> update directly. other agents -> read only; pass change facts to main agent (or user).

when updating (if authorized):
- new fact -> add one line to appropriate section in identity/hot.custom.owner.md (with date, source). no casual/transient content (persistent facts only).
- value differs from known -> confirm briefly before overwrite. e.g. "previously knew A, change to B?"
- one at a time, atomically. inform user what changed in one line after update.
- edit identity/hot.custom.owner.md only. do not touch the framework rules (rules/hot.framework.md), CLAUDE.md/AGENTS.md, or settings.

## 4. color identity gate (accent / background -- DGN-650)

agent color identity = one accent (15-color palette) + one background surface (8 allowed). spec: docs/DESIGN-SYSTEM.md R5; token canon: routines/lib/design_tokens.py.
color is NOT a standing onboarding question -- defaults resolve silently (leader -> crew accent, domain -> kit domain color, background -> crew tint; resolution order in the spec). the gate fires whenever an accent or background is being SET or CHANGED: a pick during onboarding, a later change request ("change your color"), or a crafting/mint landing a color.

MANDATORY MECHANICAL GATE -- never judge a color pick by prose. before accepting ANY accent/background value, run:

  python3 <own-root>/routines/lib/design_tokens.py --gate --accent <name> --bg <name-or-#hex> [--crew <crew>] [--leader] [--taken a,b]

- exit 0 (PASS line) -> accept. PASS carries tier + tuned anchor + chip-outline flag; the anchor is render-time data -- never store or hardcode it (computed per surface).
- exit 1 (FAIL line) -> REJECT and drive re-selection. FAIL output carries the reason + the remaining valid choices.
- gate checks, one mechanical pass: background dead-band, crew distinctiveness (leader exempt), peer distance, AA legibility tune (check spec: DESIGN-SYSTEM.md R5). accepting a pick the gate rejected = defect.
- flags: --crew when the agent belongs to a crew (omit for standalone); --leader when it is the crew leader; --taken = accents already worn by same-crew peers, when known (omit if unknown).

interaction budget (D6): selection <= 2 taps -- one numbered list for accent, one for background; defaults = 0 taps (no question asked). violation feedback is IMMEDIATE, same turn: translate the FAIL reason into user terms in the instance language (never surface command/file names), then present the gate's remaining valid candidates as a numbered list with [[OPTIONS]] on its own last line (same UI pattern as Q2). never a bare "that color does not work, pick again".

on PASS: record the confirmed pair as one line in own identity/hot.custom.agent.md Identity section, format `- Color: <accent> on <background>` -- same self-edit path as section 2. store only palette/background NAMES (or the custom bg hex); never a tuned hex.

## boundaries
- identity (identity/hot.custom.agent.md): onboarding fill + ongoing self-edit. each agent edits own only.
- profile (identity/hot.custom.owner.md): only the owner defined in RULES can edit.
- RULES/CLAUDE/settings/services: outside this skill's scope (framework baseline).
- external actions (email, restart) outside this skill's scope.
