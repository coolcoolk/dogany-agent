---
name: mint-agent
display_name: 담당 에이전트 만들기
description: Create (mint) a new dedicated domain agent through the mint verb. Triggers - "새 에이전트 만들어줘", "전담 에이전트 만들어줘", "전담 비서 하나 파줘", "봇 새로 만들어줘", "에이전트 신설", "민팅해줘", "mint a new agent", "create dedicated agent". Return-leg messages - "새 봇 만들고 왔어", "인사하고 왔어", "이름 정하고 왔어" - route to mint status or mint next, not a legacy procedure. Status/query - "민팅 상태" - routes to mint status. Pack browsing - "어떤 에이전트 팩 있어", "공식 에이전트 뭐 있어", "마켓플레이스 보여줘" - routes to mint open --browse. Weekly recommendation decline triggers - "추천 그만해", "왜 추천 안 해", "추천 이력 보여줘" - remain owned by the recommendation-state scripts. Proactive recommendation is NEVER model-initiated; only weekly_eval.py may initiate it. Side effects: the mint verb runs mint machinery and owns config/mint-flow.json; recommendation-state scripts own config/minting-state.json.
---

# mint-agent

If the conversation is about minting, hand the owner's input to the mint verb unchanged. Use `open`, `next`, `status`, or `cancel`. When the machine reports that it sent a screen, add nothing.

Use `open` only when the owner's own words in this turn ask to make a new agent. A status remark, a report that something finished, a return-leg message, or talk about another topic is not an open, even right after a mint conversation; if a flow may be active, use `status`. Never rebuild a make request from earlier turns.

Run the real entry point from the instance root. The command picks the consume checkout (`.dogany-consume/`, a consuming instance) when it exists, else the canonical tree:

```bash
bash "$(test -f .dogany-consume/scripts/pack/mint_flow.sh && echo .dogany-consume/)scripts/pack/mint_flow.sh" --instance-root <instance-root> open --input <owner-input> --fit <pack-id>[,<pack-id>...]|consult|none [--lead <one-line-lead>]
bash "$(test -f .dogany-consume/scripts/pack/mint_flow.sh && echo .dogany-consume/)scripts/pack/mint_flow.sh" --instance-root <instance-root> next --input <owner-input> [--asked <your-question>] [--more] [--fit <pack-id>|none] [--lead <one-line-lead>]
bash "$(test -f .dogany-consume/scripts/pack/mint_flow.sh && echo .dogany-consume/)scripts/pack/mint_flow.sh" --instance-root <instance-root> status
bash "$(test -f .dogany-consume/scripts/pack/mint_flow.sh && echo .dogany-consume/)scripts/pack/mint_flow.sh" --instance-root <instance-root> cancel
```

When the owner asks what agents are available rather than asking to make one, use `open --browse --lead <one-line-lead>` with no `--input`. This also holds during a consult: the machine shows every available agent and keeps the answers already given.

Pass the owner's original text unchanged in `--input`, including a button label or a complete BotFather message. Do not compose, render, repeat, translate, or supplement machine screens. Do not run legacy mint pipeline, catalog, cleanup, or screen commands. `--asked` and `--more` belong only to a consult answer; `--fit` belongs to a make request (`open --input`), a consult answer, and the owner's own words at a choice; leave them out otherwise.

Make request (`open --input`): judge the request itself and pass `--fit`. Compare the owner's words in this turn with each available agent's `name_ko` and `tagline_ko` (the `packs` fact; run `open --input` without `--fit` once to get it -- nothing is written or sent). The request clearly names one agent: pass its `id` with a confirm `--lead`; the machine sends the lead and that agent's card in one message, and the owner's tap confirms. Several agents fit the words: pass their ids joined by commas (`--fit a,b`) with a `--lead` that asks which one. The request does not tell which agent (for example only "make me an agent"): pass `--fit consult`; only then does the consult below happen. Nothing fits: pass `--fit none` (the flow ends as `no_fit`). Judge only from the owner's own words; never guess a purpose they did not say.

Consult (`waiting_for: "context"`, `note_for_agent: "ASK_OWNER"`, `agent_utterance: "consult"`): nothing was sent (a `--fit consult` make request, a restart, a browse pick, or owner words at a choice with `--more`). Ask the owner, in your own words, what situation or need the new agent is for. Ask openly: offer no candidate purposes, domains, features, or agent names, and do not guess the answer for them. Pass the owner's reply unchanged with `next --input`, with the question you asked as `--asked`; an `open --input` during a consult counts as that answer and needs the same flags. Add `--more` only when you will ask one follow-up question. `answers_left` is how many answers the machine still takes; when it is 1, the next answer closes the consult even with `--more`, so that call needs `--fit`.

`waiting_for: "context"` with `agent_utterance: "none"` (for example after `status`): your question is already out. Do not ask it again; pass the owner's next reply with `next` as above.

Fit (`--fit`): required on a make request (above) and on the `next` that closes the consult. On a consult close you judge it the same way: compare what the owner said in this flow (the request and the consult answers) with each available agent's `tagline_ko`, and pass that agent's `id`, or `none` when no agent fits. Judge only from the owner's own words. Never count words you put in the owner's mouth: a candidate you named, a purpose you suggested, or the owner agreeing to a leading question. When the owner's words are not enough to judge, ask the follow-up with `--more` instead of guessing. The owner confirms a named agent by tapping it; `none` ends the flow as `no_fit`. After a browse pick, `--fit` is not needed.

If the result has `note_for_agent: "FIT_REQUIRED"` (exit code 3), nothing was sent and the answer was not used: judge from `packs` (`id`, `name_ko`, `tagline_ko` of every available agent) and rerun the same command with `--fit` as `rerun_with` shows (`<id>`, several ids joined by commas, `consult` on a make request only, or `none`). `fit_problem: "unknown"` means an id you passed is not one of `packs`: rerun with ids from `packs` or with `none`. Say nothing to the owner in between.

Owner words at a choice (`waiting_for: "choice"`): the card invites the owner to say it in their own words when it is not what they meant. When the owner answers with their own words instead of a button, pass them unchanged with `next --input` and judge `--fit` again from all the owner's words in this flow, with a new `--lead`; or add `--more --asked <your-question>` to ask one follow-up first. `--fit none` ends the flow as `no_fit`. Talk that is not about the agent being made is not a mint input.

After a browse pick, the machine asks the consult before the token step. If the owner's answer shows the picked agent is not what they want, do not pass the answer with `next`; run `cancel`. If that same answer asks for a different agent, then run `open --input` with the answer unchanged.

Lead (`--lead`): required on every step that sends a choice screen -- a make request or a consult close with agent ids in `--fit`, owner words at a choice with agent ids, and `open --browse`; the machine checks it once, keeps it, and sends it at the top of the same message as the card; it ignores it when the step sends no choice screen. For one agent the lead is a confirm: one question that checks your reading of the owner's request by naming that agent, then that a ready "Dogany 공식 팩" exists for it (say it exactly so in Korean: the product name is Dogany, never 도가니). It does not describe what the agent does. For several agents the lead asks which one. Reflect only what the owner said in this flow and the name of the agent you judged. Never state a purpose, goal, scope, or feature the owner did not say, and never take words from agent descriptions (tagline, features). No procedure, instructions, verdicts, machine state, or secrets; one line, at most 200 characters.

Wording (every owner-facing sentence about making a new agent in this flow: talk before open, consult question, lead, buttons and option labels, follow-ups): the owner-facing verb for making an agent is 만들기 ("만들기", "만들어요", "만들 수 있어요"); never 생성, 제작, 신설 or 민팅 as the owner-facing verb. Do not use internal vocabulary -- 틀, 팩, 템플릿, 민팅, or any other term of this machinery -- unless the same sentence says in plain words what it is. The one exception is the confirm lead saying that a "Dogany 공식 팩" exists, the owner-approved term (product name Dogany, never 도가니). Say plainly what the owner is choosing.

If the result has `note_for_agent: "LEAD_REQUIRED"` (exit code 3; `lead_problem` is `missing` or `rejected`), nothing was sent and the owner input was not used: rerun the same command with a new `--lead` as `rerun_with` describes. `rejected` with a non-empty `lead_unsaid` lists words the lead carried that the owner never said: write a lead without them. Say nothing to the owner in between.

The verb writes one JSON result. `screen_delivered: true` with `note_for_agent: "SCREEN_SENT_END_TURN"` means the machine sent the complete owner screen: say nothing else and end the turn. Otherwise follow `waiting_for`: `context` means the consult above; `choice`, `confirm`, or `token` means pass the next owner input unchanged with `next`; `none` means do not invent an input. `state` is the machine state. `note_for_agent` is the fixed action code. `agent_utterance` is the permission to speak: `none` means say nothing, and only these codes permit speech:

- `N13`: the approved delivery fallback, said exactly as the block below and nothing else.
- `EXPLAIN`: no screen was sent. Tell the owner the `explain.why` and `explain.next` in your own words -- no field names, no internal terms, no fixed copy -- and do not re-run the command.
- `consult`: ask the consult question above. Only an `ASK_OWNER` result grants it.
- `no_fit`: nothing was sent and the flow ended. Tell the owner, in your own words, that no ready agent fits what they described. Do not propose a specific agent, do not offer to build one, and do not say one is being prepared. You may mention that they can ask to see the available agents.
- `occupied`: nothing was sent and the flow ended. Tell the owner an agent for this already exists (the `occupied.agent` fact), point them to it, and stop. Do not ask what else they need and do not start a new mint.

<!-- OWNER-SAY -->
> 화면 전달 상태를 확인하지 못했어요. 잠시 후 다시 요청해 주세요.
<!-- /OWNER-SAY -->

For a return-leg or status request, use `status`; if the owner supplies an input for an active flow, use `next`. For a new request to make an agent, use `open`. For an explicit cancellation, use `cancel`. Weekly recommendation decline/history requests stay with `scripts/minting_state.py` and `scripts/weekly_eval.py`; do not turn them into a mint procedure.
