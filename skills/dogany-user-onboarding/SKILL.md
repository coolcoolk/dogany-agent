---
name: dogany-user-onboarding
description: When a new agent first wakes up (an ONBOARDING_PENDING marker in AGENT.md, or name/tone/humor level still unset), proactively ask the user for its setup, fill the answers directly into the identity fields of its own AGENT.md, then delete the onboarding block. Also the procedure for ongoing self-update - when identity (name, form of address, tone, humor, emoji) changes, self-edit own AGENT.md continuously; when the user's persistent profile facts change, update USER.md. Recall this on a SessionStart "onboarding needed" signal, when my identity looks empty, when the user first tells me about themselves, or when previously known info has changed.
---

# dogany-user-onboarding — onboarding (identity self-fill) + identity/profile self-update

Single source for onboarding = the onboarding block at top of own AGENT.md. Question set lives there only.
Answers -> fill identity fields in own AGENT.md directly. done -> delete that block.
Identity (name, address, tone, humor, emoji) = ongoing self-edit target in own AGENT.md after onboarding.
User's persistent profile facts -> USER.md separately (ownership/edit rights per RULES).

## 1. onboarding (once)

### trigger condition
SessionStart hook (`routines/onboarding-check.py`) injects "onboarding needed" signal into context.
判定 criterion: own AGENT.md contains `<!-- ONBOARDING_PENDING -->` marker (or AGENT.md itself absent).
marker absent -> already onboarded, do not trigger.

### procedure
on signal -> speak first in opening conversation.

important — one question at a time only. ask one, wait for answer, then move to next. never list multiple questions at once.

also: at first awakening, no name yet, no address term for user. do not introduce self with a specific name ("OOO입니다" forbidden). ask user to give a name first. use no address term until user provides one (no-address mode). do not assume any persona (humor level etc.) before set.

order:
1. my name — ask user to name this assistant.
2. my emoji — after name decided, present 3-4 signature emoji candidates as short numbered list (e.g. "1. 🦊"). note user can pick one or send any emoji directly. put [[OPTIONS]] marker on very last line. do not ask "should I use an emoji?" (using emoji = assumed; ask which one).
3. address term — ask how to address the user. do not pre-assume any term. do not say "회원님"/"사용자"/"user" — omit object entirely: "제가 어떻게 부르면 좋을까요?"
4. tone — ask preferred communication tone (e.g. clean/polite, casual/friendly). humor level = separate next question.
5. humor level — after tone answer received, ask separately. direct: "유머 수치를 몇 %로 설정할까요?" (e.g. 10%, 30%).

do not ask:
- communication preference (answer format) — already defined in RULES.md Output/notation.
- timezone/language — auto-detected at mint/install time.
- job/email etc — do not ask now; update USER.md when they come up in conversation (see section 3).

tone when asking: clean and polite, short. no preamble or filler — greeting + question = 1-2 sentences. no bold (double-asterisk), no quote/backtick overuse, no empty phrases.

### on answer received (fill own AGENT.md directly)
fill received answers into the corresponding fields in own AGENT.md. five fields:
- Identity `Name` (`<AGENT_NAME>`)
- Identity `Emoji` (`<EMOJI>`)
- Relationship `Call the user` address term (`<FORM_OF_ADDRESS>`)
- Relationship `Tone` (`<TONE>`)
- Relationship `Humor` (`<N>`)

(also fill name in the `You are ... "<AGENT_NAME>"` line at top of AGENT.md. language/role/bot ID etc. already filled at mint time — do not touch.)

all five filled -> delete the onboarding comment block + `<!-- ONBOARDING_PENDING -->` marker line from AGENT.md top. this deletion = onboarding complete marker. skipping deletion -> triggers re-onboarding every session — must delete.

after saving, brief confirm to user: saved settings, will interact this way from now. 1-2 lines.

AGENT.md is @imported into constitution -> new identity applies from next session. no separate handoff needed.

## 2. identity change (ongoing, own AGENT.md)

after onboarding, if user asks to change identity (e.g. "이름 바꿔줘", "유머 좀 낮춰", "호칭은 ~로", "이모지 바꿔") -> edit the corresponding field in own AGENT.md directly.
identity (name, address, tone, humor, emoji) = ongoing self-edit in own AGENT.md — do without delegation.

rules:
- edit only own AGENT.md identity fields. do not touch RULES.md, CLAUDE.md, settings (those = dev-agent scope).
- one at a time, atomically. after edit, inform user what changed in one line.
- AGENT.md = @import -> auto-applied next session.

## 3. user profile update (USER.md)

new persistent user profile info learned in conversation, or existing value changed -> update USER.md.
examples:
- name, address term, timezone, email, contact
- job, affiliation, side work, role change
- habits, preferences, work rules

USER.md ownership/edit rights per RULES.md. main agent -> update directly. other agents -> read only; pass change facts to main agent (or user).

when updating (if authorized):
- new fact -> add one line to appropriate section in USER.md (with date, source). no casual/transient content (persistent facts only).
- value differs from known -> confirm briefly before overwrite. e.g. "previously knew A, change to B?"
- one at a time, atomically. inform user what changed in one line after update.
- edit USER.md only. do not touch constitution body (AGENT/CLAUDE) or settings.

## boundaries
- identity (AGENT.md): onboarding fill + ongoing self-edit. each agent edits own only.
- profile (USER.md): only the owner defined in RULES can edit.
- RULES/CLAUDE/settings/services: outside this skill's scope (dev-agent scope).
- external actions (email, restart) outside this skill's scope.
