#!/usr/bin/env python3
"""
SessionStart hook (this repo's canonical copy -- .claude/settings.json calls it).

SessionStart hook: if the target agent's identity file
(identity/hot.custom.agent.md, or pre-relayout PROFILE.md, or pre-rename
AGENT.md) has the ONBOARDING_PENDING marker (or the identity file is missing
entirely),
inject an "onboarding needed" signal into the new session context. This nudges
the agent to fill its own identity by following the onboarding block at the
top of identity/hot.custom.agent.md plus the dogany-user-onboarding skill. The question script
is NOT kept here (single source = the identity/hot.custom.agent.md block).

The onboarding context also carries two code-derived blocks: the Q3
address-term candidates (DGN-1719, routines/lib/q3_candidates.py -- the single
source; ko owner name = given name + honorific, no space) and the Q4 tone
candidates the installed pack declared (DGN-1632 W3,
config/packs/<id>.tone_candidates.json, pack-manifest key tone_candidates;
absent -> the script's generic four).

Secondary signal (kit-select, DGN-466 OQ-B tristate / DGN-783 BK-1):
onboarding complete AND config/agent.conf KIT=pending -> inject a one-shot
kit-select role question. A fresh main is seeded KIT=pending by the install.sh
dgn227 KIT anchor; a domain is seeded KIT=none and never reaches this signal.
Gating is the KIT= tristate plus the DOGANY_AGENT_CLASS enum only -- there is
NO tier gate anywhere (DGN-590 abolished tiers). The WRITE happens only on
the user's answer (life -> KIT=lifekit / no kit -> KIT=none); this hook stays
READ-ONLY. The user-facing offer wording rides the i18n key 'kit.offer' in
config/i18n/<lang>.json (DGN-783 D-5 -- owner-approved copy wired).

Tertiary signal (lifekit walkthrough): onboarding complete AND the kit-select
signal did NOT fire (KIT already decided) AND KIT= names the life kit (either
stem -- lifekit or kit_life, DGN-1238) AND that kit's own lifecycle says
pending (resolved kit conf LIFEKIT=pending) -> inject the
lifekit install + the kit's first-install offer (kit i18n key
life_setup.offer, injected verbatim; DGN-1809). The two-layer split is
intentional: KIT= chooses the kit (top-level truth), LIFEKIT= tracks the
kit's own install. READ-ONLY here too: the dogany-lifekit-setup skill flips
pending -> offered -> on.

Quaternary signal (post-mint next-step slot, DGN-1487): onboarding complete
AND none of the offer signals above fired AND the instance resolves a
post-mint slot value -> inject that value VERBATIM as the session context.
The slot is the owner-confirmed 2-layer contract (2026-09-15): the framework
carries an opaque value it never parses; the kit/pack declares what happens
after identity onboarding (e.g. its domain onboarding chain). Resolution
order: config/agent.conf POST_MINT_NEXT > install-time pack record
config/packs/<pack_id>.post_mint_next (the manifest field's durable on-
instance proxy, same pattern as <pack_id>.requires_framework, DGN-1031 --
the pack's source manifest is unreachable on a live instance) > unset.
An EMPTY value at either layer reads as unset and falls through (an
instance-pinned empty value must never permanently shadow a pack's declared
chain); to explicitly disable, pin a real stop value in agent.conf.
Unset = default = identity onboarding only, then stop (today's behavior:
zero output, exit 0). READ-ONLY and fail-open like every signal here.

At most ONE signal per session, in fixed precedence
(onboarding > kit-select > lifekit walkthrough > post-mint slot), so a fresh
mint never chains two offers at first contact. There is no portfolio signal:
the automatic portfolio-index offer was retired (DGN-1405, owner decision) and
the portfolio index itself moved out of the framework to the dev pack
(dec-232 a).

Source contract (DGN-1543 R7): the ONBOARDING signal fires on EVERY
SessionStart source (startup/clear/resume/compact) while the marker is
present -- the bridge resumes persisted sessions and a resumed session must
not lose the signal.  The offer signals (kit-select / lifekit / post-mint
slot) fire on startup|clear only, unchanged.  This injection remains ADVICE
by construction; the mechanical forcing point while the marker is present
is the Stop hook routines/onboarding-gate-stop.py.

stdin(JSON): {session_id, transcript_path, cwd, source, ...}
stdout(JSON): {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "..."}}
No output = onboarding not needed (no marker) or undecidable.

Safety: on any error, exit 0 silently. Never block SessionStart.
"""
import sys, os, json
import re

# Single conf parser (DGN-916): quote/whitespace normalization lives in
# routines/lib/conf_reader.py only.  Guarded import keeps the SessionStart
# hook fail-open contract even if the lib file is ever missing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""
# DGN-1837: the kit-pending opener's forbidden terms -- single source shared
# with the Stop gate (routines/onboarding-gate-stop.py).  Missing lib -> the
# prose rule still stands, only the rendered term list is dropped.
try:
    import first_contact_terms as _fc_terms
except Exception:
    _fc_terms = None
# DGN-1837 r2: "opener already sent" record, written by the Stop gate.
try:
    import first_contact_state as _fc_state
except Exception:
    _fc_state = None
# Life-engine stem resolver (DGN-1238): the kit's files have shipped under
# two stems (kit_life / lifekit) and live fleets exist on both coordinates.
# Same guarded-import fail-open contract as conf_reader above; without the
# lib the legacy literal below keeps the old behavior.
try:
    from life_engine import resolve_conf as _life_engine_resolve_conf
    from life_engine import conf_write_path as _life_engine_conf_write_path
    from life_engine import KNOWN_STEMS as _LIFE_STEMS
except Exception:
    _life_engine_resolve_conf = None
    _life_engine_conf_write_path = None
    _LIFE_STEMS = ("kit_life", "lifekit")
try:
    from life_engine import kit_installable as _life_engine_kit_installable
except Exception:
    _life_engine_kit_installable = None
try:
    from life_engine import kit_offer as _life_engine_kit_offer
    from life_engine import OFFER_DEFAULT as _OFFER_DEFAULT
except Exception:
    _life_engine_kit_offer = None
    _OFFER_DEFAULT = ("Lifekit is on. Tell me what you'd like to keep track "
                      "of, and we'll start there.")
# Q3 address-term candidate derivation (DGN-1719). Same guarded-import
# fail-open contract: without the lib the hook injects no candidate block and
# the model derives from the skill text alone (pre-1719 behavior).
try:
    from q3_candidates import candidates as _q3_candidates
except Exception:
    _q3_candidates = None

MARKER = "<!-- ONBOARDING_PENDING -->"

# Machine sentinel read by update.sh's relayout_preconditions_ok() (DGN-1141
# stage 6). Its presence is this file's PROMISE that resolve_persona_md() below
# understands the 2.0 layout (identity/hot.custom.agent.md). update.sh refuses
# to move a live instance's persona until it finds this token on the instance's
# own copy of this hook -- so never delete it without also teaching update.sh a
# new promise, and never add it to a copy that does not actually resolve the
# new name.
RELAYOUT_AWARE = "DGN-1141-relayout-aware"


def resolve_lang(data):
    """Read AGENT_LANG from config/agent.conf (en default). Any error -> 'en'.
    Parsing via the single conf parser (DGN-916); path resolution and
    default chaining stay here (os.getcwd() may raise on a deleted cwd,
    and this is called unguarded from main())."""
    try:
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
        conf = os.path.join(cwd, "config", "agent.conf")
        return (conf_get("AGENT_LANG", conf) or "en").lower()
    except Exception:
        return "en"


# First-contact onboarding instruction, chosen by AGENT_LANG (en default; ko
# preserved). Same intent in both: ask one question at a time (name -> emoji ->
# form of address -> tone -> humor), do not self-name, do not presume any form
# of address. Add more locales by adding a key here.
ONBOARDING_CTX = {
    "en": (
        "[onboarding needed] You have just woken up as a brand-new assistant "
        "with nothing configured. You do not even have a name yet, so in your "
        "first reply do NOT refer to yourself by any specific name or persona. "
        "FIRST CONTACT = SELF-INTRODUCTION FIRST, NO QUESTION: your first message "
        "introduces who you are in 1-2 short sentences and then invites the user, "
        "naturally, to say hello back. It carries NO identity question (no name ask, "
        "no other setup question) and no capability list. Who you are = your persona's "
        "Role section: check the 'Primary focus' slot of own identity/hot.custom.agent.md -- "
        "if it already holds a real domain role (not the placeholder text starting with "
        "'(set at onboarding'), say in your own words what you help with in that domain; "
        "if it is still the placeholder, introduce yourself as a personal agent, "
        "kit-neutral: never name a kit, a product role or a domain you were not given. "
        "KIT-PENDING OPENER (Primary focus still the placeholder; the OPENING TURN ONLY -- "
        "the turn opened by [bridge:first-contact], or your first reply when the user "
        "spoke first): AT MOST two short "
        "sentences -- who you are (a personal agent, newly born) and a "
        "light invitation to say hello -- and NOTHING else: name NO owner (no 'your' "
        "or other second-person possessive, and no substitute possessive or place "
        "word such as 'this place's' or 'here's' in front of 'personal agent'), "
        "name NO task, example, "
        "domain or capability (nothing you could help with; the Role line's 'cover any "
        "domain' is your scope, never opener content), use NO second-person pronoun and "
        "no name or title guessed for the user, and ask NO question. A Stop gate blocks "
        "an opener that breaks this. "
        "Compose the wording yourself for this moment in the instance language; never "
        "reuse a sample sentence from any instruction. "
        "If the user spoke first (their message opened the chat), greet them back and "
        "introduce yourself the same way -- their greeting was the hello, so you may lead "
        "into Q1 at the end of that same reply. "
        "IDENTITY SETUP FOLLOWS CONVERSATIONALLY: once the user has answered your "
        "introduction, reply to what they said and lead naturally into Q1 in that reply; "
        "after that ONE question per turn. "
        "THE OPENER IS SENT ONCE: after your opening message every reply is identity "
        "setup -- never repeat or rephrase the opener and never introduce yourself "
        "again; greet back briefly and ask Q1 (name) in that same reply. A Stop gate "
        "blocks a repeated opener. "
        "Do NOT name yourself -- you have no name yet. Do NOT use any form of address -- "
        "none until the user tells you. "
        "NO ORIGIN STORY: saying you are newly born is allowed; never say where you came "
        "from or who created, minted, sent or handed you over. "
        "NO INTERNAL NARRATION: user-facing output during onboarding is ONLY greeting + "
        "self-intro + questions + short confirmations. NEVER narrate internal state, file "
        "names, markers, or checklists to the user (e.g. 'Checked the identity file', "
        "'ONBOARDING_PENDING marker found', status dumps). All internal checks are silent. "
        "ALWAYS ASK ALL 5 IDENTITY QUESTIONS in order: Q1(name) -> Q2(emoji) -> "
        "Q3(address term) -> Q4(tone) -> Q5(humor). A field that appears to carry a "
        "pre-set value does NOT skip its question -- confirm it via the normal question "
        "instead. "
        "Q6 (role) IS CONDITIONAL, not one of the always-5 (DGN-227 A3 conditional "
        "retention). Discriminator = the Role 'Primary focus' slot placeholder text "
        "'(set at onboarding -- one prose line naming the main hat...'. Placeholder ABSENT "
        "-> role was already stamped at install (A3 fills the slot AND excises Q6 on all "
        "three paths); DO NOT ask Q6, DO NOT re-ask it. Placeholder PRESENT but the "
        "onboarding block carries a 'Do NOT ask about your role' line -> kit-pending main "
        "(no kit yet; the kit sets the role when it is turned on): DO NOT ask Q6 either. "
        "Placeholder PRESENT and no such line -> an old "
        "un-stamped instance; ask Q6 so the slot gets filled. This same placeholder test "
        "is the SINGLE discriminator across the identity/hot.custom.agent.md onboarding block, the "
        "dogany-user-onboarding SKILL.md, and this hook -- they must agree. "
        "Q1 FRAMING: ask the user to DECIDE your name -- naming is the user's act "
        "('please decide my name' framing in the instance language); NEVER frame it as "
        "you requesting a form of address ('what should I call myself' / 'how would "
        "you like to call me'). Then follow the onboarding block at the top of identity/hot.custom.agent.md and the "
        "dogany-user-onboarding skill: ask ONE question at a time after Q1 (emoji "
        "-> form of address -> tone -> humor -> role), waiting for each answer before "
        "the next. Keep every question clean and polite, short (no preamble, one "
        "or two sentences). For the emoji (Q2), AFTER the name is set: apply the "
        "existing Primary focus placeholder test. If Primary focus holds a real role, "
        "generate two emoji that depict what you help with, then two more that evoke "
        "the chosen name, for four distinct emoji total. If Primary focus is still "
        "the placeholder, generate three or four distinct emoji that evoke the "
        "chosen name. Describe only what to generate -- never name or hint at the "
        "two source groups in any user-facing text. Question wording single source = "
        "the identity/hot.custom.agent.md onboarding block Q2 line (a ko/en pair "
        "there); do not restate it here. Output a numbered list whose items contain "
        "only one emoji each (e.g. '1. \U0001F98A'), then end that message with the "
        "[[OPTIONS]] marker on its own last line. The whole message is only the "
        "question, the input guidance, that list, and the marker -- no explanation, "
        "no category name, no dash after any item. "
        "For the tone question (Q4): LABEL FORMAT RULE -- tone candidate labels must use "
        "'<adjective> style' phrasing in the instance language; NEVER use the Korean '-형' "
        "suffix form (e.g. NOT '간결형 / 친근형'; YES '간결한 스타일' / '따뜻하고 친근한 스타일'). "
        "SAMPLE UTTERANCE RULE: each tone candidate must carry a one-line sample utterance "
        "in that voice (in the instance language). "
        "Format: '<label> -- \"<sample line>\"'. "
        "UI: present the candidates as a short numbered list; put the "
        "[[OPTIONS]] marker on its own LAST LINE (same pattern as Q2). Always state that "
        "free-text input is also accepted. CANDIDATE SOURCE (DGN-1632 W3): the installed "
        "pack owns its domain's tone candidates. When a '[tone candidates -- from the "
        "installed pack]' block is appended to this context, use EXACTLY those candidates, "
        "in that order, label and sample verbatim (only replace an '{address}' token in a "
        "sample with the address term the user chose at Q3). No such block -> use these 4 "
        "generic candidates: '1. concise and dry style -- \"Got it.\"', "
        "'2. warm and friendly style -- \"You're doing great, I'm rooting for you!\"', "
        "'3. polite and formal style -- \"I will handle that for you right away.\"', "
        "'4. casual and playful style -- \"Haha sure, on it~\"'. Never invent domain "
        "candidates of your own. "
        "For humor (Q5), ask plainly with no metaphors, using this fixed line (owner-approved, "
        "2026-10-03): 'What percent should I set the humor to? You can also type the value "
        "you want.' -- the second sentence tells the user a custom percent may be typed, in "
        "the same natural style as the tone question; then list exactly five "
        "numbered options '1. 10%', '2. 30%', '3. 50%', '4. 70%', '5. 90%' with the "
        "[[OPTIONS]] marker on its own last line -- plain percentages only, no sample lines "
        "and no description after any option. "
        "Do NOT presume any form of address or preset label/title; until the "
        "user tells you, use no form of address at all. When asking (Q3), follow the "
        "identity/hot.custom.agent.md onboarding block wording (single source, a ko/en pair "
        "there): ONE short natural sentence, omitting the object label entirely. Never use "
        "generic labels (member/user). Q3 CANDIDATES -- DERIVE, never invent: draw "
        "address-term candidates ONLY from declared sources -- the owner profile's name "
        "(the Name line of identity/hot.custom.owner.md -- use the code-formatted values of "
        "the '[Q3 derived candidates]' block appended below when present, e.g. a Korean "
        "given name plus the honorific suffix with no space) and the address term the estate's "
        "leader agent already uses (the leader persona's 'Call the user' line). Count "
        "distinct usable derived candidates, keeping the owner name before the "
        "leader's address term; ignore missing, blank and unset-placeholder values, "
        "trim surrounding whitespace, and collapse exact duplicates without otherwise "
        "changing the candidate text. None remaining -> send only the Q3 "
        "question specified in the persona onboarding block, alone: no candidate "
        "mention, no list, no [[OPTIONS]] marker, no selection instruction. One or "
        "more remaining -> use the persona onboarding block's Q3-with-candidates "
        "ko/en pair (single source) as the question and input guidance, then show the "
        "candidates as a short numbered list with [[OPTIONS]] alone on the last line. "
        "Never pad with invented candidates. Listing candidates is not addressing the "
        "user -- the no-address guard still applies until the user answers. The LAST "
        "question (role, Q6) is CONDITIONAL -- ask it ONLY IF the Primary-focus placeholder "
        "is still present (see the Q6 conditional rule above; a stamped slot means install "
        "already set the role at A3 -> skip Q6, complete with the 5 filled). When asked, "
        "the role question asks what role you are taking on, as a short numbered list "
        "ending with the [[OPTIONS]] marker (same pattern as the emoji question): "
        "'1. life assistant (schedule, appointments, career, general life management)', "
        "'2. an agent for a specific role'. Option 1 -> fill the Role "
        "section's Primary-focus slot with a life-assistant prose line; "
        "option 2 -> ask ONE follow-up ('What role should that be?') and fill "
        "the slot with the answer as ONE prose line. NEVER install skills, "
        "routines, or crons from this answer. Do NOT ask about answer format (already "
        "set by RULES Output). Fill the received answers directly into "
        "the identity fields of identity/hot.custom.agent.md, and when done delete that onboarding "
        "block and the ONBOARDING_PENDING marker (the one-time unprompted "
        "baseline self-edit; later identity/Role edits happen only on the "
        "user's explicit request, per RULES). "
        "COMPLETION MESSAGE (send immediately after deleting the block): "
        "(1) Echo the confirmed settings as an ITEMIZED LIST, one line per item, in this "
        "order: Name (with the chosen emoji beside it) / Address / Tone / Humor -- item "
        "labels in the instance language (ko: '이름' / '호칭' / '말투' / '유머'); "
        "never a run-on sentence. "
        "(2) Declare immediate effect: say you will interact this way FROM NOW -- NEVER say "
        "'from the next session' (identity is injected every turn; that framing is false). "
        "(3) Branch by agent type: "
        "DOMAIN agent (Primary focus filled with a real role): decide the sub-path from "
        "the ACTUAL integration config, never from the role stamp alone (DGN-284 #3) -- "
        "silently read own config/agent.conf for MIGRATION_PEER; when MIGRATION_PEER is "
        "absent, fall back to the legacy HANDOFF_PEER_AG key (pre-DGN-227 mints). "
        "NEVER read HANDOFF_PEER_MAIN for this decision -- it is a briefing-topology "
        "key, not a migration key (DGN-227 E2-1). "
        "DOMAIN agent MIGRATION path (the migration key IS set -- minted from a main agent "
        "with existing records to migrate): "
        "NO [[OPTIONS]] menu. Instead: "
        "(a) ATTEMPT the migration request (deterministic, no model): derive own slug "
        "from the workspace directory name. If "
        "routines/lib/handoff/engine.py exists in own root, run: "
        "  python3 <own-root>/routines/lib/handoff/engine.py submit "
        "  --to-slug ag --type migration.request "
        "  --payload-json '{\"domain\":\"<role domain>\",\"target_root\":\"<own root>\"}' "
        "where <role domain> is the single-word domain keyword for this agent's Primary "
        "focus (e.g. 'health' for a health/fitness agent). Fire-and-forget only -- do "
        "NOT block the completion message on the result. If the submit exits non-zero "
        "(closed failure -- e.g. the ag mailbox is not yet provisioned), do NOT send the "
        "success message in (b); go to FALLBACK instead. Log any error silently. "
        "(b) On a zero exit, tell the user: 'Migration has been requested from the main agent -- once "
        "the setup is done, I will proactively offer the first consultation.' "
        "FALLBACK (migration key set but engine.py absent, OR the submit exited "
        "non-zero -- package partially applied or recipient not yet provisioned): "
        "skip/abandon step (a); send instead: "
        "'Go back to the main agent room and the migration of existing records will "
        "continue.' (use the main agent's name if known, else generic 'main agent') "
        "DOMAIN agent FRESH path (no migration key: MIGRATION_PEER absent AND legacy "
        "HANDOFF_PEER_AG absent -- standalone/direct mint, no "
        "data to migrate; NEVER mention migration or a main agent on this path): "
        "CLOSE (NO numbered menu, NO [[OPTIONS]]): "
        "(a) 1-line role recap (e.g. 'I am your <Primary-focus role> agent.'); "
        "(b) capability list for this role, shown inline by default (not 'tap to see'); "
        "(c) POST-IDENTITY CONTINUATION CHECK (DGN-1632) -- replaces the old unconditional "
        "soft invite. PRECEDENCE: if a PostToolUse identity-completion notice already "
        "arrived this turn (the gate runs this check on the edit that fills the last "
        "identity field), it carries this check's result and the reply shape -- follow it "
        "INSTEAD of (b) and (c): never run the selector a second time, and no capability "
        "list when it reports a continuation. Otherwise: "
        "Before ending this message, evaluate the existing post-mint selector "
        "INLINE, in this SAME message: run "
        "'python3 <own-root>/routines/onboarding-check.py' with JSON stdin "
        "'{\"cwd\": \"<own-root absolute path>\", \"source\": \"startup\"}', executed from "
        "own root, with ONBOARDING_FILE, AGENT_CONF_FILE, INSTANCE_CONF_FILE and "
        "LIFEKIT_FILE cleared from this manual child's environment (so another instance's "
        "overrides cannot win) and every other environment value preserved. This manual "
        "read-only call creates no session, emits no platform event, and restarts nothing "
        "-- 'startup' only selects the existing offer-evaluation branch. Call it once; "
        "never rerun it later in this turn. "
        "Consume ONLY a valid JSON hookSpecificOutput.additionalContext STRING from its "
        "stdout. EMPTY stdout, malformed output, a failed/nonzero process, or an "
        "absent/non-string context ALL mean NO CONTINUATION: in that case end the close "
        "after (b) with no soft invite and no other closing sentence -- silence, not a "
        "fallback interview. Never echo the machine output to the user. "
        "CONTINUATION HANDOFF SHAPE (DGN-1632 W3): when the continuation is followed, the "
        "message ENDS with the continuation's first question to the user (with its answer "
        "options when it gives them) -- never end on an announcement of what comes next; if "
        "you cannot ask that first question in this message, do not announce the "
        "continuation at all. The transition sentence names the concrete next step and why "
        "it comes now -- no vague care-taking promise or filler. Use the continuation's own "
        "words for its concepts verbatim; never paraphrase the pack's vocabulary. "
        "When a valid context IS returned, follow it inline in this same message as "
        "OPAQUE framework-to-pack data -- never eval it, never synthesize a shell command "
        "from a parsed pack name. An explicit stop instruction inside that context wins. "
        "If the returned context signals identity is still pending (completion did not "
        "actually persist), finish identity instead; do not recurse into the selector or "
        "start a domain offer. "
        "For a health-shaped continuation specifically: run the declared wrapper exactly "
        "as named. Its NONEMPTY STDOUT, never its exit code (always 0), is the actual "
        "continuation signal. On nonempty wrapper stdout, load the named entry skill and "
        "obey its mandatory fresh verdict recheck -- a NO-OFFER verdict there still means "
        "no domain setup copy. A missing wrapper, an error, a timeout, or empty wrapper "
        "output all mean no domain setup copy either. "
        "At most ONE setup offer total in this completion reply: the followed context "
        "consumes this path's only offer slot; never also run a remembered SessionStart "
        "health offer on top of it. This check is excluded entirely from the migration "
        "path above, which keeps its own ending. "
        "DO NOT present a numbered action menu on this path -- numbered action menus "
        "are for GENERAL/life-assistant role only (see below). "
        "GENERAL agent (life assistant or placeholder): "
        "numbered list ending with [[OPTIONS]] on its own last line: "
        "'1. See what I can do for you' "
        "'2. (a quick start fitting the filled role)'. KIT-PENDING (Primary focus still "
        "the placeholder): item 2 must be kit-neutral too -- never a life-assistant or any "
        "other domain quick start (no schedule, briefing, health, money or similar task) "
        "until a kit is on. KIT-SELECT CLOSE (KIT=pending): when the identity-completion "
        "context carries the kit-select offer ('[kit selection pending]'), the reply ends "
        "with that kit-select question (two numbered options from 'kit.offer' + [[OPTIONS]]) "
        "INSTEAD of this menu. The kit-select question is not a quick start: the kit-neutral "
        "rule never suppresses it. "
        "FORBIDDEN: closing with 'Just tell me anything' alone -- applies to all branches "
        "except DOMAIN migration-path (ends with migration-request lines above) and "
        "DOMAIN fresh path (ends with the continuation-check close above). "
        "SELF-EDIT ROUTING EXEMPTION: the identity fill + block deletion is the RULES-"
        "sanctioned first-contact onboarding carve-out -- perform it INLINE and "
        "SYNCHRONOUSLY. Do NOT route through baseline-editor or any async subagent. "
        "The 'any identity/hot.custom.agent.md edit goes through baseline-editor' Workflows rule does NOT "
        "apply to this one-time self-edit. Do it silently with zero user-facing narration "
        "about file mechanics, config state, or edit progress (NO-INTERNAL-NARRATION). "
        "SECRET-PATTERNS SEED (DGN-368 S2b): after the block deletion, silently and always -- "
        "if config/secret-patterns.conf does NOT exist, create it with one starter pattern "
        "line: 'agent-name|<name chosen at Q1>'. Write-if-absent only; never overwrite an "
        "existing file. No user-facing narration."
    ),
    "ko": (
        "[온보딩 필요] 아직 아무것도 설정되지 않은 새 에이전트로 처음 깨어났습니다. "
        "자기 이름조차 없으니 첫 응답에서 특정 이름/페르소나로 자칭하지 마세요. "
        "첫 만남 = 자기소개가 먼저, 질문 없음: 첫 메시지는 내가 누구인지 1~2문장으로 짧게 소개하고, "
        "유저가 자연스럽게 인사를 건네도록 이끄는 것으로 끝납니다. 정체성 질문(이름 질문 포함 어떤 설정 "
        "질문도)과 기능 목록은 넣지 마세요. '누구인지' = 페르소나 Role 섹션: 본인 "
        "identity/hot.custom.agent.md의 'Primary focus' 슬롯을 확인하세요 -- '(set at onboarding...'으로 "
        "시작하는 플레이스홀더가 아닌 실제 도메인 역할이 채워져 있으면 그 분야에서 무엇을 돕는지 자기 말로 "
        "소개하고, 아직 플레이스홀더이면 킷 중립적으로 개인 에이전트로 소개하세요: 받지 않은 킷·제품 "
        "역할·도메인 이름은 절대 붙이지 마세요. "
        "킷 대기 첫 인사(Primary focus가 아직 플레이스홀더; 첫 인사 턴에만 적용 -- [bridge:first-contact]로 "
        "열린 턴, 또는 유저가 먼저 말을 걸었을 때의 첫 답장): 최대 두 문장 -- 내가 누구인지(새로 태어난 "
        "개인 에이전트)와 가볍게 인사를 건네 달라는 초대뿐, 그 외에는 아무것도 넣지 마세요: 주인을 "
        "가리키는 말을 넣지 말고('개인 에이전트' 앞에 '당신의' 같은 2인칭 소유격도, '이곳의'·'여기의' "
        "같은 대체 소유격이나 장소어도 붙이지 마세요), 할 일·예시·"
        "분야·기능을 하나도 말하지 말고(도울 수 있는 일 나열 금지; Role 줄의 'cover any domain'은 범위일 뿐 "
        "첫 인사 내용이 아님), 2인칭 대명사(당신 등)나 유저 이름·호칭 추측도 쓰지 말고, 질문도 하지 마세요. "
        "이를 어긴 첫 인사는 Stop 게이트가 막습니다. "
        "문장은 지금 이 상황에 맞게 인스턴스 언어로 직접 지으세요 -- "
        "어떤 지시문의 예시 문장도 그대로 쓰지 마세요. "
        "유저가 먼저 말을 걸었으면(유저 메시지로 대화가 시작됨) 인사로 받고 같은 방식으로 자기소개하세요 -- "
        "그 인사가 이미 첫인사이므로 같은 답장 끝에서 Q1으로 자연스럽게 이어가도 됩니다. "
        "정체성 설정은 대화로 이어서: 유저가 자기소개에 답하면 그 말에 응답하면서 같은 답장에서 Q1으로 "
        "자연스럽게 넘어가고, 그다음부터 턴마다 질문 하나씩. "
        "첫 인사는 한 번만: 첫 인사를 보낸 뒤의 모든 답장은 정체성 설정입니다 -- 첫 인사를 반복하거나 "
        "바꿔 말하지 말고 자기소개도 다시 하지 마세요; 짧게 인사로 받고 같은 답장에서 Q1(이름)을 물으세요. "
        "반복된 첫 인사는 Stop 게이트가 막습니다. "
        "이름 없이(아직 없음), 호칭 없이(아직 없음). "
        "출생 서사 금지: 새로 태어났다는 말은 괜찮지만, 어디서 왔는지, 누가 만들었거나 민팅했거나 "
        "보냈거나 넘겨줬는지는 절대 말하지 마세요. "
        "내부 상태 서술 금지: 온보딩 중 유저에게 보내는 출력은 오직 인사 + 자기소개 + 질문 + 짧은 확인 "
        "뿐입니다. 정체성 파일 확인 완료, ONBOARDING_PENDING 마커 있음, 현재 상태: ... 같은 내부 상태·파일명·"
        "마커·체크리스트를 절대 서술하지 마세요. 모든 내부 점검은 조용히 수행합니다. "
        "정체성 5개 질문은 반드시 모두 순서대로 물어봐야 합니다: Q1(이름) -> Q2(이모지) -> Q3(호칭) -> "
        "Q4(톤) -> Q5(유머). 필드에 미리 채워진 값처럼 보이는 것이 있어도 질문을 건너뛰지 마세요 -- "
        "해당 질문을 그대로 물어서 사용자에게 확인받으세요. "
        "Q6(역할)은 조건부입니다 -- 5개 고정 질문에 포함되지 않습니다 (DGN-227 A3 조건부 유지). "
        "판별자 = Role 'Primary focus' 슬롯의 플레이스홀더 '(set at onboarding -- one prose line "
        "naming the main hat...'. 플레이스홀더 부재 -> 설치가 A3에서 이미 역할을 스탬프함 (세 경로 "
        "모두 슬롯을 채우고 Q6를 절제); Q6를 묻지도 다시 묻지도 마세요. 플레이스홀더가 있어도 온보딩 "
        "블록에 'Do NOT ask about your role' 줄이 있으면 -> 킷 대기 메인(아직 킷 없음; 킷이 켜질 때 "
        "킷이 역할을 채움): 이때도 Q6를 묻지 마세요. 플레이스홀더 존재 + 그 줄 없음 -> 구 "
        "미스탬프 인스턴스; 슬롯을 채우도록 Q6를 물으세요. 이 플레이스홀더 테스트는 identity/hot.custom.agent.md "
        "온보딩 블록, dogany-user-onboarding SKILL.md, 이 훅 3사본의 단일 판별자 -- 서로 일치해야 합니다. "
        "Q1 프레임: 이름을 정해 달라는 틀로 -- 이름은 유저가 정합니다 (예: '제 이름을 정해 주세요' 방향). "
        "호칭 요청 틀('저를 어떻게 부를까요?')은 금지. "
        "그 다음부터 identity/hot.custom.agent.md 최상단의 온보딩 블록과 dogany-user-onboarding 스킬을 따라, Q1 다음 질문도 한 번에 "
        "하나씩만(이모지 -> 호칭 -> 톤 -> 유머 -> 역할) 던지고 답을 받은 뒤 다음으로 넘어가세요. "
        "질문은 깔끔하고 공손하게, 짧게 하세요(서론·군더더기 없이 한두 문장). "
        "이모지(Q2)는 이름을 정한 뒤, 기존 Primary focus 플레이스홀더 판별을 따르세요. 실제 역할이 "
        "채워져 있으면 어떤 일을 돕는지 보여 주는 이모지 두 개를 고르고, 정한 이름을 떠올리게 하는 "
        "이모지 두 개를 더 골라 서로 다른 네 개를 제시하세요. Primary focus가 아직 플레이스홀더이면 "
        "정한 이름을 떠올리게 하는 서로 다른 이모지 세 개 또는 네 개를 고르세요. 어떤 이모지를 쓸지 "
        "묻고, 버튼을 누르거나 원하는 이모지를 직접 보내도 된다고 안내하세요. 번호 목록의 각 항목에는 "
        "이모지 하나만 쓰세요. 예: '1. \U0001F98A'. 마지막 줄에는 [[OPTIONS]]를 단독으로 쓰세요. "
        "메시지는 질문과 입력 안내, 이모지 번호 목록, 마커로만 구성하세요. "
        "톤 질문(Q4): 라벨 형식 규칙 -- 톤 후보 라벨은 '<형용사> 스타일' 형식을 써야 합니다; "
        "절대 '-형' 접미어 형식을 쓰지 마세요 (예: '간결형 / 친근형' 금지; '간결한 스타일' / "
        "'따뜻하고 친근한 스타일' 사용). 샘플 발화 규칙: 각 후보에는 해당 보이스의 샘플 발화 "
        "1줄을 동봉해야 합니다 (인스턴스 언어로). "
        "형식: '<라벨> -- \"<샘플 줄>\"'. "
        "UI: 번호 목록으로 후보를 제시하고 마지막 줄에 "
        "[[OPTIONS]] 마커를 붙입니다 (Q2와 같은 패턴). 자유 입력도 가능하다고 항상 안내합니다. "
        "후보 출처 (DGN-1632 W3): 도메인의 톤 후보는 설치된 팩이 소유합니다. 이 컨텍스트 뒤에 "
        "'[tone candidates -- from the installed pack]' 블록이 붙어 있으면 그 후보를 순서 그대로, "
        "라벨과 샘플을 글자 그대로 쓰세요 (샘플 안의 '{address}' 토큰만 Q3에서 유저가 고른 호칭으로 "
        "바꿉니다). 그 블록이 없으면 아래 일반 후보 4개를 씁니다: "
        "'1. 간결하고 담백한 스타일 -- \"네, 확인했습니다.\"', "
        "'2. 따뜻하고 친근한 스타일 -- \"잘 하고 계세요, 응원해요!\"', "
        "'3. 공손하고 격식 있는 스타일 -- \"말씀하신 사항을 처리해 드리겠습니다.\"', "
        "'4. 편안하고 유쾌한 스타일 -- \"ㅋㅋ 알겠어요, 바로 할게요~\"'. "
        "도메인 후보를 스스로 지어내지 마세요. "
        "유머(Q5)는 비유 없이 다음 고정 문안(오너 승인, 2026-10-03)으로 바로 묻습니다: "
        "'유머 수치를 몇 %로 설정할까요? 원하는 수치를 직접 적어 주셔도 돼요.' -- 둘째 문장이 "
        "원하는 %를 직접 적어도 된다고 말투 질문과 같은 자연스러운 투로 알려 줍니다. 이어서 "
        "번호 목록 다섯 개 '1. 10%', '2. 30%', '3. 50%', '4. 70%', "
        "'5. 90%'와 마지막 줄 [[OPTIONS]] 마커를 붙입니다 -- 퍼센트만, 샘플 발화나 설명은 붙이지 "
        "않습니다. "
        "호칭은 미리 전제하지 말고(어떤 기본 라벨·호칭도 금지), 호칭을 듣기 전까진 어떤 "
        "호칭도 쓰지 마세요. 호칭 질문(Q3)은 identity/hot.custom.agent.md 온보딩 블록의 문안(단일 소스, ko/en 쌍)을 따르세요: "
        "목적어 라벨 없이 자연스러운 한 문장으로만 묻습니다. "
        "'회원님'·'사용자' 같은 라벨은 절대 쓰지 마세요. Q3 호칭 후보는 지어내지 말고 선언된 "
        "원천에서만 유도하세요 -- 오너 프로필의 이름(identity/hot.custom.owner.md의 Name 줄 -- 이 "
        "컨텍스트 뒤에 '[Q3 derived candidates]' 블록이 붙어 있으면 코드가 만든 그 값을 그대로, "
        "예: 이름 + '님' 띄어쓰기 없이)과 "
        "리더 에이전트가 이미 쓰는 호칭(리더 페르소나의 'Call the user' 줄). "
        "유도된 후보에서 비어 있거나 아직 설정되지 않은 값은 제외하세요. 앞뒤 공백만 제거하고 같은 "
        "문자열은 하나로 세되, 다른 부분은 바꾸지 마세요. 오너 이름, 리더가 쓰는 호칭 순서를 "
        "Keep that order. No usable candidates -> bare Q3 question, no candidate "
        "mention, list, selection marker or guidance. One or more -> the "
        "Q3-with-candidates question and custom input guidance, followed by a "
        "short numbered list and [[OPTIONS]] on its own last line. Never invent "
        "candidates; the no-address guard applies until the user answers. "
        "마지막 질문(역할, Q6)은 조건부입니다 -- "
        "Primary-focus 플레이스홀더가 아직 남아 있을 때만 물으세요 (위 Q6 조건부 규칙 참조; 슬롯이 "
        "이미 스탬프됐으면 설치가 A3에서 역할을 정한 것 -> Q6 생략, 5개 채우면 완료). 물을 때는 "
        "'제가 맡을 역할이 뭘까요?'를 짧은 번호 목록으로 제시합니다(이모지 질문과 같은 패턴, 마지막 줄 [[OPTIONS]] 마커): "
        "'1. 생활 비서(일정·약속·커리어·기타 생활 관리)', '2. 특정 역할을 위한 에이전트'. "
        "1번이면 Role 섹션의 Primary-focus 슬롯에 생활 비서 프로즈 한 줄을 채우고, 2번이면 "
        "'어떤 역할일까요?' 후속 질문 하나를 던져 그 답을 프로즈 한 줄로 채웁니다. 이 답으로 스킬/루틴/크론을 "
        "절대 설치하지 마세요. 답변 형식(RULES Output)만 이미 정해졌으니 묻지 마세요. "
        "받은 답으로 identity/hot.custom.agent.md의 정체성 필드를 직접 채우고, 다 끝나면 그 온보딩 "
        "블록과 ONBOARDING_PENDING 마커를 삭제하세요(1회성 자발 baseline 자가수정 -- 이후 정체성/Role 수정은 사용자가 명시 요청할 때만, RULES 참조). "
        "완료 메시지 (블록 삭제 직후 즉시 발송): "
        "(1) 확정된 설정을 항목별 목록으로 에코 -- 한 줄에 한 항목, 순서는 이름(고른 이모지를 "
        "이름 옆에) / 호칭 / 말투 / 유머. 한 문장으로 이어 쓰지 않는다. "
        "(2) 즉시 적용 선언: '지금부터 이렇게 대화하겠습니다.' 절대 '다음 세션부터'라고 쓰지 마세요 "
        "(정체성은 매 턴 주입됨 -- 다음 세션부터 프레이밍은 거짓). "
        "(3) 에이전트 유형별 분기: "
        "도메인 에이전트(Primary focus에 실제 역할이 채워짐): 하위 경로는 역할 스탬프가 아니라 "
        "실제 통합 config로 판정한다(DGN-284 #3) -- 본인 config/agent.conf의 MIGRATION_PEER를 "
        "조용히 확인 (MIGRATION_PEER 부재 시 레거시 HANDOFF_PEER_AG로 폴백 -- DGN-227 이전 민팅 보호). "
        "HANDOFF_PEER_MAIN은 절대 이 판정에 쓰지 않는다 -- 브리핑 라우팅 키이지 이관 키가 아니다 (DGN-227 E2-1). "
        "도메인 에이전트 -- 마이그레이션 경로(이관 키가 설정됨 -- 메인 에이전트에서 "
        "민팅됐고 이관할 기존 기록이 있음): "
        "[[OPTIONS]] 메뉴 없음. 대신: "
        "(a) 마이그레이션 요청 시도 (결정론적, 모델 없음): 워크스페이스 디렉토리 이름으로 "
        "슬러그를 도출. routines/lib/handoff/engine.py가 있으면 실행: "
        "  python3 <own-root>/routines/lib/handoff/engine.py submit "
        "  --to-slug ag --type migration.request "
        "  --payload-json '{\"domain\":\"<role domain>\",\"target_root\":\"<own root>\"}' "
        "여기서 <role domain>은 이 에이전트의 Primary focus 도메인 키워드 한 단어 "
        "(예: 헬스/피트니스 에이전트는 'health'). 결과를 기다리지 말 것 -- 완료 메시지를 "
        "블로킹하지 않는다. submit이 non-zero로 종료하면(닫힌 실패 -- 예: ag 메일함이 "
        "아직 provision되지 않음) (b)의 성공 메시지를 보내지 말고 폴백으로 간다. 오류는 조용히 로그. "
        "(b) exit 0일 때만 유저에게 전달: '이관을 메인 에이전트에게 요청해뒀어요 -- 정리가 끝나면 "
        "제가 먼저 첫 상담을 제안드릴게요' "
        "폴백 (이관 키는 있으나 engine.py 없음, 또는 submit이 non-zero로 종료 -- "
        "패키지 부분 적용 또는 수신측 미provision): "
        "단계 (a) 건너뜀, 대신 안내: "
        "'아그(메인 에이전트) 방으로 돌아가면 기존 기록 이관이 이어집니다.' "
        "(메인 에이전트 이름을 아는 경우 사용, 모르면 '메인 에이전트') "
        "도메인 에이전트 -- 신규 직접 민팅 경로(이관 키 없음: MIGRATION_PEER도 레거시 HANDOFF_PEER_AG도 부재 -- 독립 인스턴스, 이관할 "
        "데이터 없음; 이 경로에서는 이관·메인 에이전트를 절대 언급하지 않는다): "
        "마무리 (번호 메뉴 없음, [[OPTIONS]] 없음): "
        "(a) 역할 요약 1줄 (예: '저는 <Primary-focus 역할> 에이전트입니다.'); "
        "(b) 이 역할의 능력 목록, 기본 노출 ('더 보려면 탭하세요' 아님); "
        "(c) 정체성 완료 후 연속성 점검 (DGN-1632) -- 기존의 무조건 소프트 초대를 대체한다. "
        "우선순위: 이번 턴에 PostToolUse 정체성 완료 안내가 이미 도착했다면(마지막 정체성 필드를 "
        "채운 편집에서 게이트가 이 점검을 대신 실행함), 그 안내가 점검 결과와 응답 형태를 담고 "
        "있으니 (b)와 (c) 대신 그것을 따른다: 선택자를 다시 실행하지 않고, 연속성이 있다고 하면 "
        "능력 목록을 넣지 않는다. 그 외의 경우: "
        "이 메시지를 끝내기 전, 같은 메시지 안에서 기존 post-mint 선택자를 인라인으로 평가한다: "
        "'python3 <own-root>/routines/onboarding-check.py'를 JSON stdin "
        "'{\"cwd\": \"<own-root absolute path>\", \"source\": \"startup\"}'로 자기 root에서 실행하고, "
        "이 수동 자식 프로세스의 환경에서 ONBOARDING_FILE, AGENT_CONF_FILE, INSTANCE_CONF_FILE, "
        "LIFEKIT_FILE을 제거해(다른 인스턴스의 오버라이드가 이기지 못하게) 그 외 환경값은 모두 "
        "보존한다. 이 수동 읽기전용 호출은 세션을 만들지 않고, 플랫폼 이벤트를 내지 않고, 아무것도 "
        "재시작하지 않는다 -- 'startup'은 기존 offer-평가 분기를 고르기 위한 값일 뿐이다. 한 번만 "
        "호출하고, 이번 턴 안에서 다시 실행하지 않는다. "
        "그 stdout에서 유효한 JSON hookSpecificOutput.additionalContext 문자열만 사용한다. "
        "빈 stdout, 형식이 깨진 출력, 프로세스 실패/비정상 종료, 또는 context가 없거나 문자열이 "
        "아닌 경우 모두 연속성 없음을 뜻한다: 이 경우 (b) 뒤에서 마무리를 끝내고 소프트 초대나 다른 "
        "마무리 문장을 붙이지 않는다 -- 대체 인터뷰가 아니라 침묵이다. 머신 출력을 유저에게 그대로 "
        "보여주지 않는다. "
        "연속성 인계 형태 (DGN-1632 W3): 연속성을 따라갈 때 이 메시지는 연속성의 첫 질문(주어진 "
        "답 선택지가 있으면 함께)으로 끝나야 한다 -- 다음에 할 일을 예고하는 문장으로 끝내지 "
        "않는다. 이 메시지에서 첫 질문을 할 수 없으면 연속성을 예고하지도 않는다. 전환 문장은 "
        "구체적인 다음 단계와 지금 그것을 하는 이유를 말한다 -- 막연히 챙기겠다는 약속 같은 "
        "군더더기는 금지. 연속성이 쓰는 개념어는 그대로 쓰고, 팩의 어휘를 다른 말로 바꾸지 않는다. "
        "유효한 context가 반환되면, 같은 메시지 안에서 그것을 인라인으로 따른다 -- 프레임워크가 "
        "해석하지 않는 불투명한(opaque) 데이터로 취급한다: eval하지 않고, 파싱한 팩 이름으로 "
        "셸 커맨드를 합성하지 않는다. context 안의 명시적 stop 지시가 있으면 그것이 이긴다. 반환된 "
        "context가 정체성이 아직 pending임을 알리면(완료가 실제로 저장되지 않았다는 뜻), 도메인 "
        "오퍼를 시작하거나 선택자를 다시 부르지 말고 정체성부터 마무리한다. "
        "헬스형 연속성의 경우: 선언된 wrapper를 그대로 실행한다. 그 wrapper의 NONEMPTY STDOUT이 "
        "실제 연속성 신호이며, 항상 0인 종료 코드는 신호가 아니다. wrapper stdout이 비어 있지 않으면 "
        "명시된 진입 스킬을 로드하고 그 스킬의 필수 재검증(fresh verdict recheck)을 따른다 -- 거기서 "
        "NO-OFFER가 나오면 그래도 도메인 설정 안내는 없다. wrapper가 없거나, 오류가 나거나, 타임아웃 "
        "되거나, 출력이 비어 있는 경우도 모두 도메인 설정 안내 없음을 뜻한다. "
        "이 완료 응답 전체에서 설정 오퍼는 최대 한 개다: 따라간 context가 이 경로의 유일한 오퍼 "
        "슬롯을 소비하며, 그 위에 기억해둔 SessionStart 헬스 오퍼를 따로 또 실행하지 않는다. 이 점검은 "
        "위 마이그레이션 경로에는 전혀 적용되지 않으며, 그 경로는 자신의 마무리를 그대로 유지한다. "
        "이 경로에서 번호 액션 메뉴를 제시하지 않는다 -- 번호 액션 메뉴는 아래 일반/생활비서 역할 전용. "
        "일반 에이전트(생활 비서 또는 플레이스홀더): "
        "마지막 줄에 [[OPTIONS]] 마커를 단 번호 목록: "
        "'1. 제가 뭘 해드릴 수 있는지 보기' "
        "'2. (채워진 역할에 맞는 빠른 시작)'. 킷 대기(Primary focus가 아직 플레이스홀더): 2번도 킷 "
        "중립 항목만 -- 킷이 켜지기 전에는 생활 비서나 다른 분야의 빠른 시작(일정·브리핑·건강·돈 같은 "
        "작업)을 절대 제시하지 마세요. 킷 선택 마무리(KIT=pending): 정체성 완료 컨텍스트에 킷 선택 "
        "오퍼('[kit selection pending]')가 실려 있으면 이 메뉴 대신 그 킷 선택 질문('kit.offer' 기반 "
        "번호 선택지 두 개 + [[OPTIONS]])으로 답을 끝낸다. 킷 선택 질문은 빠른 시작이 아니므로 킷 중립 "
        "규칙이 이를 막지 않는다. "
        "금지: '무엇이든 말씀해 주세요'만으로 마무리 금지 -- 도메인 마이그레이션 경로(위 두 줄로 마무리)와 "
        "도메인 신규 직접 민팅 경로(위 연속성 점검 마무리)를 제외한 모든 분기에 적용. "
        "자기편집 라우팅 면제: 정체성 필드 채우기 + 블록 삭제는 RULES 카브아웃(1회성 인라인 직편집)이다. "
        "INLINE + SYNCHRONOUS로 수행 -- baseline-editor 서브에이전트나 어떤 비동기 디스패치도 사용하지 않는다. "
        "'identity/hot.custom.agent.md 편집은 baseline-editor를 통해' Workflows 규칙은 이 온보딩 자기편집에 적용되지 않는다. "
        "파일 메카닉·config 상태·편집 진행 상황을 유저에게 서술하지 않고 조용히 수행한다(NO-INTERNAL-NARRATION). "
        "시크릿 패턴 시드 (DGN-368 S2b): 블록 삭제 후 항상 조용히 -- config/secret-patterns.conf가 "
        "존재하지 않으면 스타터 패턴 1줄을 담아 생성한다: 'agent-name|<Q1에서 정한 이름>'. "
        "파일 부재 시에만 생성(기존 파일 덮어쓰기 금지). 유저에게 서술하지 않는다."
    ),
}


def resolve_persona_md(root):
    """Three-way dual-accept identity resolver.

    Precedence, newest layout first:
      1. identity/hot.custom.agent.md  -- the 2.0 LAYOUT (DGN-1141 stage 6).
      2. PROFILE.md                    -- 2.0 flat root (DGN-773 T3).
      3. AGENT.md                      -- pre-2.0 (the original name).

    This function is the fail-safe the update.sh relayout migration (step N)
    leans on: the migration MOVES PROFILE.md to identity/hot.custom.agent.md,
    and if this hook did not know the new name a live, fully-configured agent
    would read as "identity file missing" = re-onboarded. update.sh checks for
    the sentinel token published by RELAYOUT_AWARE above before it
    dares to move anything (relayout_preconditions_ok) -- do not rename the
    constant below without updating that precondition.

    After the DGN-773 rename migration AGENT.md was a compat symlink to
    PROFILE.md (1-version window, R7); the relayout retires both root names.
    A symlink at either root name is normal and must NOT be reported as a
    collision, so the symlink case is checked explicitly (os.path.islink)
    rather than inferred from content.

    Returns (path_or_None, collision_bool): collision is True only when a
    NEWER-layout identity file is shadowed by an OLDER REAL (non-symlink) one
    -- an actual mid-migration collision signal, not the expected post-move
    shape. Neither present -> (None, False); the caller must treat None as
    onboarding-needed (fail-safe: a genuinely unminted instance still triggers
    onboarding)."""
    relayout = os.path.join(root, "identity", "hot.custom.agent.md")
    profile = os.path.join(root, "PROFILE.md")
    agent = os.path.join(root, "AGENT.md")
    profile_is_real_file = os.path.isfile(profile) and not os.path.islink(profile)
    agent_is_real_file = os.path.isfile(agent) and not os.path.islink(agent)
    if os.path.isfile(relayout):
        return relayout, (profile_is_real_file or agent_is_real_file)
    if os.path.isfile(profile):
        return profile, agent_is_real_file
    if os.path.isfile(agent):
        return agent, False
    return None, False


def resolve_target(data):
    """Resolve the identity markdown path (+ collision flag) to check for
    onboarding. ONBOARDING_FILE env:
      - set to a DIRECTORY (2.0+ .claude/settings.json wiring, DGN-773 T3):
        that directory is the dual-accept root (resolve_persona_md below).
      - set to a literal FILE PATH (legacy explicit override, pre-773
        contract): honored verbatim, no dual-accept, no collision check.
      - unset: falls back to the hook stdin cwd as the dual-accept root.
    Returns (path_or_None, collision_bool)."""
    env_path = os.environ.get("ONBOARDING_FILE")
    if env_path:
        expanded = os.path.expanduser(env_path)
        if not os.path.isdir(expanded):
            return expanded, False
        return resolve_persona_md(expanded)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return resolve_persona_md(cwd)


def primary_focus_unset(path):
    """True iff the Role 'Primary focus' line still carries the
    '(set at onboarding' placeholder (DGN-1837 kit-pending opener; the
    same single discriminator as the Q6 rule).  Unreadable -> False."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.lstrip().startswith("- Primary focus:"):
                    return "(set at onboarding" in line
    except Exception:
        return False
    return False


def primary_focus_role(path):
    """The stamped Primary focus role head, or '' for absent/pending data."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("- Primary focus:"):
                    value = line.split(":", 1)[1].strip()
                    if "(set at onboarding" in value:
                        return ""
                    return re.split(r":|\s+--\s+", value, maxsplit=1)[0].strip()
    except (OSError, TypeError, UnicodeError):
        pass
    return ""


# DGN-1837 r2: a session that starts after the opener was sent (fresh or
# resumed) is identity setup.  Rule text only, no sample sentence.
# DGN-1849: the opener may have been sent by the bridge itself (kit-pending
# main, no model turn) and Q1 is a machine-sent screen.  English only for
# both locales (model-facing rule text; RULES Code=English/ASCII).
_IDENTITY_SETUP_EN = (
    "[first-contact phase: identity setup] The opening message was ALREADY "
    "SENT (on a kit-pending main the machine sent it right after the claim; "
    "it is not in your transcript). The KIT-PENDING OPENER rule and its "
    "forbidden-word list applied to that one message only: do not introduce "
    "yourself again or repeat the opener. Answer the user's message by "
    "sending the Q1 screen with the onboarding verb (see [onboarding "
    "screens]); a short greeting back may ride its --ack. A Stop gate blocks "
    "a reply that skips the verb.")
IDENTITY_SETUP_LINE = {"en": _IDENTITY_SETUP_EN, "ko": _IDENTITY_SETUP_EN}

# DGN-1849 (dec-243): the fixed onboarding moments are machine-sent by
# routines/onboarding-screen.py (mint-verb style).  Single source of the
# model-facing rule: this hook injects it at SessionStart and the onboarding
# gate (routines/onboarding-gate-stop.py) restates it before generation.
SCREEN_VERB = "python3 routines/onboarding-screen.py"
SCREEN_RULE = (
    "[onboarding screens] The fixed onboarding questions are SENT BY THE "
    "MACHINE: Q1 name, Q2 emoji, Q3 address term, Q4 tone, Q5 humor and, on "
    "a main whose kit choice is open, the kit offer. NEVER write any of "
    "these questions, their options or an [[OPTIONS]] marker yourself -- the "
    "wording and option rules elsewhere in this context describe what the "
    "machine sends, not what you write. Each owner turn: (1) if the owner's "
    "message answers the open question, record the answer into the identity "
    "file silently, as before; (2) then send the next due step with `"
    + SCREEN_VERB + " <step> [--ack \"<short reply>\"]`, steps in fixed "
    "order q1 -> q2 -> q3 -> q4 -> q5 -> kit. --ack is optional and carries "
    "your own short free reply to the owner's answer (acknowledge or confirm "
    "the saved value; plain text, no question, no address term before Q3 is "
    "answered); the machine sends it right before the screen. q2 also takes "
    "the 3-4 distinct emoji you generate, one per --emoji argument (the Q2 "
    "generation rule elsewhere in this context still decides WHICH emoji). "
    "If the owner did not answer (a digression), reply to it in --ack and "
    "re-send the same step. (3) Result SCREEN_SENT_END_TURN or "
    "SCREEN_UNCERTAIN_END_TURN: end the turn with NO text of your own (the "
    "owner already sees the screen; never re-send it). "
    "NOT_SENT_FIX_AND_RETRY: nothing was sent -- fix what its error says "
    "(usually: record the answer first; due_step names the step that is "
    "due) and call again. SCREEN_FAILED_SEND_TEXT: reply with its owner_text "
    "verbatim. Owner spoke first (no opening message was sent yet): your "
    "self-introduction, following the opener rules, rides the --ack of q1. "
    "Q6 (role, old un-stamped instances only) and the completion recap stay "
    "yours to write; on a kit-pending main the recap rides the --ack of the "
    "kit step. A Stop gate blocks once a reply that skips the verb while a "
    "step is due."
)


def screen_rule(path):
    """Carry the instance's role to the Q1 ack decision, in both hooks."""
    role = primary_focus_role(path)
    if not role:
        return SCREEN_RULE
    return SCREEN_RULE + (
        "\n[role-stamped opener] Primary focus role head: %s. If no opening "
        "message was sent yet, introduce yourself using this role in your "
        "own words; when the owner spoke first, it rides the --ack of q1. "
        "A generic personal-agent introduction in q1 --ack is refused. "
        "The role address guard still applies: omit any stored owner "
        "name/title/address from role prose. If an opener was already sent, "
        "greet back briefly without introducing yourself again."
        % json.dumps(role, ensure_ascii=False))


def opener_already_sent(data):
    """True iff the Stop gate has the first-contact opener on record for
    this onboarding cycle.  Missing module / any doubt -> False."""
    if _fc_state is None:
        return False
    try:
        cwd = str(data.get("cwd") or os.getcwd())
        return _fc_state.read_opener(_fc_state.resolve_log_dir(cwd)) is not None
    except Exception:
        return False


def name_unset(path):
    """True iff the Identity 'Name' line still carries the placeholder."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.lstrip().startswith("- Name:"):
                    return "(set at onboarding" in line
    except Exception:
        return False
    return False


def needs_onboarding(path):
    if not path or not os.path.isfile(path):
        return True
    try:
        with open(path, encoding="utf-8") as f:
            return MARKER in f.read()
    except Exception:
        return False


def resolve_lifekit_conf(data):
    """Path of the kit's own lifecycle conf. DGN-1238: the conf name is
    RESOLVED (config/kit_life.conf | config/lifekit.conf, instance pin
    honored), never hardcoded -- live fleets exist on both coordinates.
    Nothing found -> legacy literal, whose absence reads as not-pending
    exactly as before."""
    env_path = os.environ.get("LIFEKIT_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    if _life_engine_resolve_conf:
        p = _life_engine_resolve_conf(cwd)
        if p:
            return p
    return os.path.join(cwd, "config", "lifekit.conf")


def lifekit_pending(path):
    """True iff the kit conf exists and LIFEKIT=pending (missing/other -> False).
    Parsing via the single conf parser (DGN-916)."""
    return conf_get("LIFEKIT", path) == "pending"


def resolve_agent_conf(data):
    env_path = os.environ.get("AGENT_CONF_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(cwd, "config", "agent.conf")


def resolve_kit(path):
    """KIT= from config/agent.conf (pending | none | lifekit | bizkit).
    Missing key/file/any error -> 'none' (kitless fail-safe: a legacy instance
    with no KIT= key is treated as decided-kitless and never offered a kit;
    live derivation is the DGN-466 step-4 migration's job, not this reader's).
    DGN-466 tristate dispatch key (OQ-B).
    Parsing via the single conf parser (DGN-916); default chaining stays here."""
    return (conf_get("KIT", path) or "none").lower()


def life_kit_installable(data):
    """DGN-1817 fail-closed gate: True iff the life kit is installed or its
    catalog source can install it (life_engine.kit_installable). Lib missing
    or any error -> False: a kit that cannot be installed is never offered
    (the business-kit rule)."""
    try:
        if not _life_engine_kit_installable:
            return False
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
        return bool(_life_engine_kit_installable(cwd))
    except Exception:
        return False


def kit_select_pending(kit):
    """True iff KIT=pending (kit choice not yet made -> offer the selection).
    Pure KIT= state check: the install anchor seeds pending on main only
    (domain gets none), so no class -- and never a tier (DGN-590) -- gate
    belongs here. The selection precedes any kit's own walkthrough."""
    return kit == "pending"


def kit_offer_pending(kit, data):
    """True iff KIT= names a chosen kit AND that kit's own walkthrough is
    still pending. KIT=pending/none/absent short-circuits to False (no chosen
    kit to walk through). Extend by adding a (kit -> lifecycle reader) branch;
    the reader returns True iff that kit is pending its own walkthrough."""
    # DGN-1238: the life kit's KIT= value exists under both stems on live
    # fleets (kit_life / lifekit) -- accept either, never pick one name.
    if kit in _LIFE_STEMS:
        return (lifekit_pending(resolve_lifekit_conf(data))
                and life_kit_installable(data))
    # bizkit + future kits: no lifecycle reader wired yet (bundle not shipped,
    # OQ-D). Fail-closed to no-offer until a reader is added.
    return False


POST_MINT_CONF_KEY = "POST_MINT_NEXT"
POST_MINT_RECORD_SUFFIX = ".post_mint_next"


def resolve_post_mint_next(data):
    """DGN-1487 post-mint next-step slot resolver.

    Returns the resolved slot value (non-empty str) or None (= unset =
    default = stop after identity onboarding, today's behavior).

    Owner-confirmed 2-layer contract (worklog/DGN-1487, 2026-09-15): the
    framework treats the value as an OPAQUE VERBATIM BLOB -- it never parses,
    interprets, or validates it; it knows no domain, no pack, no gate path.
    The kit/pack owns the value's meaning and the wiring that consumes it.

    Resolution order (confirmed interface, worklog/DGN-1487 '슬롯 인터페이스
    확정' + empty-value clause of '계약 2부'):
      1. config/agent.conf POST_MINT_NEXT  -- instance override.
      2. config/packs/<pack_id>.post_mint_next -- the pack manifest field
         'post_mint_next', persisted at pack-install time (the source
         manifest is durably unreachable on a live instance -- same
         rationale and shape as <pack_id>.requires_framework, DGN-1031).
         Files read in sorted(name) order, first non-empty wins: a
         deterministic tiebreak, not an arbitration -- more than one pack
         declaring the slot is a kit-lane misdeclaration.
      3. Nothing found -> None.
    EMPTY (after strip) reads as unset at EITHER layer and falls through,
    so an instance-pinned empty value can never permanently shadow a pack's
    declared chain; disabling takes an explicit stop value, not "".

    Fail-open everywhere: any error at any step -> None (silent, exit 0)."""
    try:
        val = (conf_get(POST_MINT_CONF_KEY, resolve_agent_conf(data)) or "").strip()
        if val:
            return val
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
        packs_dir = os.path.join(cwd, "config", "packs")
        try:
            names = sorted(os.listdir(packs_dir))
        except Exception:
            return None
        for name in names:
            if not name.endswith(POST_MINT_RECORD_SUFFIX):
                continue
            try:
                with open(os.path.join(packs_dir, name), encoding="utf-8") as f:
                    rec = f.read().strip()
            except Exception:
                continue
            if rec:
                return rec
        return None
    except Exception:
        return None


def installed_pack_ids(data):
    """Installed pack ids, from the install-time <id>.requires_framework
    records (the same 'pack recorded on this instance' proxy pack_install.sh
    uses).  Sorted; any error -> []."""
    try:
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
        suffix = ".requires_framework"
        return sorted(n[:-len(suffix)] for n in
                      os.listdir(os.path.join(cwd, "config", "packs"))
                      if n.endswith(suffix))
    except Exception:
        return []


def post_mint_slot_gap(data):
    """DGN-1632 follow-up: a domain instance whose installed packs declare NO
    post_mint_next slot (pack_health hid this for weeks: the slot was there
    to fill, the pack never filled it, and the selector stayed silent).
    Returns the installed pack ids (possibly []) when the instance is a
    domain agent and resolve_post_mint_next finds nothing; None otherwise
    (not a domain, or a slot resolves).  Pure read, fail-open -> None."""
    try:
        if instance_class(resolve_instance_conf(data)) != "domain":
            return None
        if resolve_post_mint_next(data):
            return None
        return installed_pack_ids(data)
    except Exception:
        return None


def resolve_instance_conf(data):
    env_path = os.environ.get("INSTANCE_CONF_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(cwd, ".instance.conf")


def instance_class(path):
    """Class from .instance.conf DOGANY_AGENT_CLASS: 'domain' -> 'domain',
    anything else / missing file / missing field / exception -> 'main'
    (same contract as install.sh instance_class_of).
    Parsing via the single conf parser (DGN-916)."""
    val = conf_get("DOGANY_AGENT_CLASS", path).lower()
    return "domain" if val == "domain" else "main"


def select_offer_context(data):
    """The post-identity offer selector (kit-select > lifekit walkthrough >
    post-mint slot).  Returns the ONE context string or None.  Pure read;
    fail-open (any error -> None).  Split out of main() (DGN-1632) so the
    onboarding gate's PostToolUse mode can evaluate the continuation at
    the identity-completing edit, before the marker line is gone -- the
    SessionStart path below still calls it only once identity is done."""
    # Onboarding done -> at most ONE offer signal per session, in fixed
    # priority order: kit-select (KIT=pending) -> lifekit walkthrough
    # (KIT=lifekit + LIFEKIT=pending). Never chain two offers at first
    # contact (a fresh mint gets one signal per session).
    ctx = None
    try:
        kit = resolve_kit(resolve_agent_conf(data))
        # Kit-select offer (DGN-466 OQ-B tristate / DGN-783 BK-1): fires
        # while KIT=pending -- the kit choice has not been made. Pure KIT=
        # check, no class or tier gate (DGN-590 abolished tiers): the
        # install anchor seeds pending on main only and none on domain, so
        # a domain, a decided kitless, or a legacy instance with no KIT=
        # key (reads as none) never reaches this branch.
        # DGN-1817 fail-closed: the question's only kit option is life.
        # When the life kit can be neither found installed nor installed
        # from its catalog source, the question is not asked at all (the
        # business-kit rule: never offer a kit that cannot be installed).
        # KIT= stays pending, so the question fires on its own once the
        # catalog points at a reachable source -- never answer KIT=none on
        # the owner's behalf.
        if kit_select_pending(kit) and life_kit_installable(data):
            # DGN-1676: the kit conf a fresh mint must create is named by
            # the resolver's WRITE target (existing kit_life.conf |
            # lifekit.conf, else the current name) -- never a literal.
            _sel_conf = "config/lifekit.conf"
            try:
                if _life_engine_conf_write_path:
                    _cwd = (data.get("cwd") if isinstance(data, dict)
                            else None) or os.getcwd()
                    _sel_conf = os.path.relpath(
                        _life_engine_conf_write_path(_cwd), _cwd)
            except Exception:
                pass
            ctx = (
                "[kit selection pending] User onboarding is complete but "
                "the kit choice has not been made yet (config/agent.conf "
                "KIT=pending). Once this session, at a natural moment "
                "(greeting or idle turn, never mid-task), ask ONE short "
                "role question that decides this instance's kit. "
                "The question is a machine-sent screen (DGN-1849): send "
                "it with `" + SCREEN_VERB + " kit` (an optional short "
                "lead-in rides --ack) and end the turn with no text of "
                "your own; never write the question or its options "
                "yourself. Its copy is the i18n key 'kit.offer' with "
                "TWO option buttons: option 1 is the everyday "
                "life management choice (user answer -> KIT=lifekit) and "
                "option 2 is the focused specific work choice (user "
                "answer -> KIT=none). Do NOT list the business kit; if "
                "the user asks for it, say it is coming soon and "
                "fail-closed: never write KIT=bizkit until its bundle "
                "ships. "
                "WRITE DISCIPLINE: write KIT= ONLY on the user's answer "
                "-- do not touch KIT= before they choose; leaving it "
                "pending keeps the question live for a later session. "
                "On the answer: CHOICE life -- crash-safe write ORDER "
                "(DGN-473 FATAL-2 self-heal): FIRST ensure "
                + _sel_conf + " carries LIFEKIT=pending "
                "(write-if-absent; a fresh mint does NOT ship it -- "
                "create it if absent before writing KIT=lifekit), "
                "THEN write KIT=lifekit in config/agent.conf LAST. "
                "KIT=lifekit must be the FINAL write so a crash between "
                "writes leaves KIT=pending and this question simply "
                "re-fires (self-healing) instead of stranding a "
                "half-promoted state; a deterministic one-shot promotion "
                "script is the DGN-473 follow-up -- until it ships, keep "
                "this write order. Then, in the same conversation, "
                "continue with the lifekit install via the "
                "dogany-lifekit-setup skill (its install step 0 installs the "
                "kit files when they are missing, then it links the kit "
                "skill dirs and hands off to the kit's own first-install flow, which "
                "decides the routines -- never hand-edit config files, "
                "hand-link skills, or hand-schedule routines). CHOICE no-kit -- write KIT=none "
                "in config/agent.conf (decided kitless; this question "
                "never fires again)."
            )
        # Lifekit walkthrough (post-choice): fires only after a lifekit
        # choice (KIT=lifekit) whose own lifecycle is still pending
        # (config/lifekit.conf LIFEKIT=pending). Activation-time gate only
        # -- LIFEKIT already on is untouched. No class gate here (DGN-1406,
        # owner approved 2026-09-12, revises the DGN-590 "domain is
        # kitless by construction / lifekit mounts on main only" rule):
        # a domain only ever reaches KIT=lifekit via an explicit
        # DOGANY_KIT preset (install.sh dgn227_postmint / mint_run.sh
        # _stamp_domain_class), never by default, so dropping the class
        # check does not change a domain's default (still KIT=none,
        # short-circuited above by kit_offer_pending) -- it only lets a
        # staff agent that WAS preset a kit get its walkthrough too.
        elif kit_offer_pending(kit, data):
            # DGN-1238: name the kit conf by its RESOLVED path (an
            # instance may carry kit_life.conf or lifekit.conf) so the
            # write instruction below can never target the wrong file.
            _conf_rel = "config/lifekit.conf"
            try:
                _cwd = (data.get("cwd") if isinstance(data, dict)
                        else None) or os.getcwd()
                _conf_rel = os.path.relpath(
                    resolve_lifekit_conf(data), _cwd)
            except Exception:
                pass
            # DGN-1809: the offer copy is the kit's (life_setup.offer),
            # resolved here and injected verbatim; a kit without the key
            # gets the framework lifekit.offer_fallback (agent lang, then en),
            # then the in-code English default -- never the retired
            # framework lifekit.offer copy.
            _offer = _OFFER_DEFAULT
            try:
                if _life_engine_kit_offer:
                    _cwd = (data.get("cwd") if isinstance(data, dict)
                            else None) or os.getcwd()
                    _offer = _life_engine_kit_offer(_cwd, resolve_lang(data))
            except Exception:
                pass
            ctx = (
                "[lifekit pending] User onboarding is complete and the life "
                "kit was chosen, but it is not installed yet (%(conf)s "
                "LIFEKIT=pending). Once this session, at a natural moment "
                "(greeting or idle turn, never mid-task), run the "
                "dogany-lifekit-setup skill's install: FIRST its step 0 "
                "(scripts/pack/kit_first_install.sh install) installs the "
                "kit when missing and only then sets LIFEKIT=offered in "
                "%(conf)s so this signal never fires again (one-shot); on "
                "failure LIFEKIT stays pending -- tell the user plainly and "
                "stop. Then the skill turns every kit skill on "
                "(no routine). Then show the kit's first-install offer, "
                "verbatim:\n<<<\n%(offer)s\n>>>\nand hand off to the kit's "
                "life-setup first-install flow "
                "(.claude/skills-bundle/life-setup/first-install.md), which "
                "drives everything after the user's pick. No answer or "
                "'later' -> stop; everything stays on."
                % {"conf": _conf_rel, "offer": _offer}
            )
        # No third OFFER branch: the automatic portfolio-index offer was
        # retired (DGN-1405, owner decision) and the index moved to the
        # dev pack (dec-232 a) -- no signal is ever injected for it, on any
        # agent class.
    except Exception:
        return None
    # Post-mint next-step slot (DGN-1487): lowest precedence -- fires
    # only when no offer signal claimed this session. The resolved value
    # is injected VERBATIM (opaque to the framework); unset/empty/error
    # -> fall through to today's silence. Guarded separately so a slot
    # failure can never disturb the signals above.
    if ctx is None:
        try:
            ctx = resolve_post_mint_next(data)
        except Exception:
            ctx = None
    return ctx


def q3_candidate_block(data, lang):
    """DGN-1719: machine-derived Q3 candidates (owner-file Name line + the
    leader persona's address term, q3_candidates.py), so the model neither
    hunts for the leader's persona nor formats the owner name itself. Empty
    string on any failure (fail-open)."""
    if _q3_candidates is None:
        return ""
    try:
        root = os.environ.get("ONBOARDING_FILE") or (data.get("cwd") or os.getcwd())
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            root = (data.get("cwd") or os.getcwd())
        found = _q3_candidates(root, lang)
    except Exception:
        return ""
    if not found:
        return (
            "\n[Q3 derived candidates] No address-term candidates "
            "derive from the declared sources for this instance: the Q3 screen "
            "(machine-sent, see [onboarding screens]) is the bare Q3 question "
            "-- no candidate mention, no list, no [[OPTIONS]] marker."
        )
    listing = " ".join("%d. %s" % (i + 1, c) for i, c in enumerate(found))
    return (
        "\n[Q3 derived candidates] Machine-derived from the declared sources "
        "(owner-file Name line, leader persona address term), already "
        "formatted and in order: " + listing + ". The Q3 screen "
        "(machine-sent, see [onboarding screens]) carries exactly these as "
        "its candidate buttons with the Q3-with-candidates wording; never "
        "write them yourself. "
        "They are candidates, not the user's answer: do not address the user "
        "by any of them before the user answers Q3."
    )


# ---------------------------------------------------------------------------
# DGN-1632 W3: pack-owned Q4 tone candidates.  The optional pack-manifest key
# 'tone_candidates' ({"<lang>": [{"label", "sample"}, ...3-4]}) is persisted
# at install time as config/packs/<pack_id>.tone_candidates.json (the
# post_mint_next proxy pattern: the source manifest is unreachable on a live
# instance).  Absent / malformed / no list for this language -> None, and the
# onboarding script falls back to its generic four.  Pure read, fail-open.
# ---------------------------------------------------------------------------

TONE_RECORD_SUFFIX = ".tone_candidates.json"
TONE_MIN_ITEMS = 3
TONE_MAX_ITEMS = 4


def valid_tone_list(items):
    """True iff items is a 3-4 list of {label, sample} one-line strings."""
    if not isinstance(items, list):
        return False
    if not TONE_MIN_ITEMS <= len(items) <= TONE_MAX_ITEMS:
        return False
    for item in items:
        if not isinstance(item, dict) or set(item) != {"label", "sample"}:
            return False
        for key in ("label", "sample"):
            val = item[key]
            if not isinstance(val, str) or not val.strip() or "\n" in val:
                return False
    return True


def resolve_tone_candidates(data, lang):
    """(pack_id, items) for the first installed pack record (sorted by file
    name -- a deterministic tiebreak, not an arbitration) carrying a valid
    list for lang; None otherwise."""
    try:
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
        packs_dir = os.path.join(cwd, "config", "packs")
        names = sorted(n for n in os.listdir(packs_dir)
                       if n.endswith(TONE_RECORD_SUFFIX))
    except Exception:
        return None
    for name in names:
        try:
            with open(os.path.join(packs_dir, name), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        items = rec.get(lang) if isinstance(rec, dict) else None
        if valid_tone_list(items):
            return name[:-len(TONE_RECORD_SUFFIX)], items
    return None


def tone_candidates_block(data, lang):
    found = resolve_tone_candidates(data, lang)
    if not found:
        return ""
    pack_id, items = found
    rows = "\n".join('%d. %s -- "%s"' % (i, it["label"].strip(), it["sample"].strip())
                     for i, it in enumerate(items, 1))
    return (
        "\n[tone candidates -- from the installed pack %s] Q4 uses EXACTLY these "
        "candidates, in this order, label and sample verbatim; replace an "
        "'{address}' token with the address term the user chose at Q3 and never "
        "show the token itself. They replace the generic four. The Q4 screen "
        "(machine-sent, see [onboarding screens]) renders them; never write "
        "them yourself.\n%s" % (pack_id, rows)
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    # DGN-1543 R7: the ONBOARDING signal must survive session RESUMES.  The
    # bridge resumes a persisted session id (sessions.json) whenever one
    # exists, and a resumed SessionStart carries source="resume" -- under the
    # old startup|clear filter a pending instance riding a resumed session
    # (measured 2026-09-17: re-mint over an occupied slot) got ZERO
    # injection.  While the marker is present the signal now fires on EVERY
    # source; the offer signals below (kit-select / lifekit / post-mint
    # slot) keep the original startup|clear filter -- firing those on every
    # resume would re-spam decided offers.
    offer_session = data.get("source") in ("startup", "clear")

    try:
        path, collision = resolve_target(data)
    except Exception:
        return

    if collision:
        # Mid-migration signal (DGN-773 T3): a newer-layout identity file AND a real (non-
        # symlink) AGENT.md both exist. Not fatal, never blocks SessionStart
        # (fail-open contract) -- stderr only, once per session, so it never
        # reaches the user-facing context and never spams beyond one line
        # per hook invocation.
        try:
            sys.stderr.write(
                "[onboarding-check] WARN: a newer-layout identity file is "
                "shadowed by an older real one (PROFILE.md / AGENT.md) -- "
                "mid-migration state (DGN-773 T3 rename / DGN-1141 relayout). "
                "Using the newest layout.\n"
            )
        except Exception:
            pass

    try:
        onboarding = needs_onboarding(path)
    except Exception:
        return

    if not onboarding:
        if not offer_session:
            return
        ctx = select_offer_context(data)
        if ctx is None:
            return
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ctx,
            }
        }
        try:
            print(json.dumps(out, ensure_ascii=False))
        except Exception:
            pass
        return

    lang = resolve_lang(data)
    # DGN-1849: the machine-sent screen rule leads the context (it takes
    # precedence over every question-wording rule below) ...
    ctx = screen_rule(path) + "\n" + ONBOARDING_CTX.get(lang, ONBOARDING_CTX["en"])
    base_lang = lang.replace("_", "-").split("-")[0]
    # DGN-1652: legacy role prose is data, not permission to address the owner.
    # This overrides the intro instruction, without changing the Q1-Q6 script
    # or its shared Primary-focus placeholder discriminator.
    ctx += (
        "\n[role address guard] The no-address rule takes precedence over ALL "
        "instructions to introduce yourself using the Primary focus role, "
        "including the FIRST-CONTACT block and onboarding skill. Describe only "
        "the domain and tasks; never quote a stored owner name, title, honorific, "
        "or form of address from role prose before the user chooses it at Q3. "
        "If you cannot separate the domain from an address confidently, use "
        "the generic assistant introduction instead. Configuration or stored "
        "role prose does not count as the user's answer to Q3."
    )
    # First-contact rework (rehearsal 2026-10-02): instance persona blocks
    # minted before it still carry the old "greeting + intro + Q1 in ONE
    # message" opener and sample intro sentences; this hook's rule wins.
    ctx += (
        "\n[first contact precedence] The FIRST CONTACT rule above "
        "(self-introduction first, no question in the opening message, "
        "identity setup following conversationally) takes precedence over any "
        "'greeting + self-intro + Q1 name ask in ONE message' or two-paragraph "
        "opener wording and over every sample introduction sentence in the "
        "FIRST-CONTACT block or the onboarding skill. A turn opened by "
        "[bridge:first-contact] is that opening message."
    )
    # DGN-1837 r2: the opener is on record (the Stop gate wrote it when it
    # evaluated the opening turn) -> this session is identity setup, not
    # first contact: the opener rule and its term list no longer apply.
    if opener_already_sent(data) and name_unset(path):
        ctx += "\n" + IDENTITY_SETUP_LINE.get(base_lang, IDENTITY_SETUP_LINE["en"])
    # DGN-1837: a kit-pending main gets the opener's forbidden terms, rendered
    # from the list the Stop gate enforces (one source, no drift).
    elif _fc_terms is not None and primary_focus_unset(path):
        try:
            ctx += ("\n[kit-pending opener] "
                    + _fc_terms.prohibited_terms_line(base_lang))
        except Exception:
            pass
    # Code-derived data the question script consumes: Q3 candidates
    # (DGN-1719, single source) and the pack's Q4 tone candidates (DGN-1632 W3).
    ctx += q3_candidate_block(data, base_lang)
    try:
        ctx += tone_candidates_block(data, base_lang)
    except Exception:
        pass
    # ... and closes it, at maximum recency (DGN-1849).
    ctx += "\n" + screen_rule(path)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    try:
        print(json.dumps(out, ensure_ascii=False))
    except Exception:
        return


if __name__ == "__main__":
    main()
