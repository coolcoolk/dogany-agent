---
name: dogany-user-onboarding
description: 새 에이전트가 처음 깨어났을 때(AGENT.md에 ONBOARDING_PENDING 마커가 있거나 이름·말투·유머 농도가 미설정) 사용자에게 능동적으로 설정을 묻고, 받은 답을 자기 AGENT.md 정체성 필드에 직접 채운 뒤 온보딩 블록을 삭제하는 절차. 또한 운영 중 정체성(이름·호칭·톤·유머·이모지)이 바뀌면 자기 AGENT.md를 상시 자가수정하고, 사용자의 영속 프로필 정보가 바뀌면 USER.md를 갱신하는 절차. SessionStart에서 "온보딩 필요" 신호를 받았을 때, 내 정체성이 비어 보일 때, 사용자가 자기 정보를 처음 알려줄 때, 기존에 알던 정보가 달라졌을 때 떠올린다.
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
