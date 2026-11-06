---
name: dogany-preview-switch
display_name: 미리보기 기능 켜고 끄기
scope: framework
layer: shipped
description: Turn a preview feature on or off when the owner asks - the Why net (keeps reasons, asks before guessing), the daily retro, computer use, making a custom agent. Also answers which previews exist and whether one is on. Korean triggers - "회고 켜줘", "회고 꺼줘", "하루 회고 켜줘", "이유 기억하는 기능 켜줘", "Why 켜줘", "화면 조작 켜줘", "화면 조작 꺼줘", "나한테 맞는 에이전트 만들기 켜줘", "미리보기 기능 켜줘", "미리보기 뭐 있어", "회고 켜져 있어?". English triggers - "turn on the retro", "turn off the retro", "turn on the Why net", "turn on computer use", "turn off computer use", "turn on custom agents", "enable the preview", "what previews are there", "is the retro on". Owner asks only; never turned on by the agent's own initiative. Output = the confirm line (on), then a short result; off needs no confirm. Side effects = one KEY=value line in config/agent.conf and, on macOS, the feature's background unit loaded / unloaded via routine-ctl.sh. Tier BEST-EFFORT.
---

# dogany-preview-switch -- preview features on / off on request

Reader: agent. Tier: BEST-EFFORT (description auto-fire, no hook).
Carrier: `python3 <ROOT>/routines/preview-switch.py` (one JSON object out).
`<ROOT>` = instance root, `<ID>` = feature id below.

## Features
| id | owner words (examples) | conf key |
|----|------------------------|----------|
| `why-net` | 이유 기억, Why, 짐작 전에 물어보기 | `WHY_NET` |
| `retro` | 회고, 하루 회고, retro | `RETRO_SLOT` |
| `computer-use` | 화면 조작, computer use | `COMPUTER_USE` |
| `custom-agent` | 나한테 맞는 에이전트 만들기, custom agent | `CUSTOM_AGENT` |
- Map the owner's words to ONE id. Unsure which -> ask which one, with the
  list. Never guess.
- Words name no feature on the list -> run `on <their word>` anyway: exit 2
  `unknown_feature` returns `known`. Tell the owner it is not a preview
  feature and name the known ones in plain words. Change nothing else.

## Turn on (owner asked)
1. `preview-switch.py on <ID>` -> exit 10, `confirm_line`. Send it
   verbatim. End the turn.
2. Owner replies -> `preview-switch.py on <ID> --confirm "<reply verbatim>"`.
   - exit 10 `not_a_yes` -> the reply was not a plain yes. Owner said no /
     later -> say it stays off, stop. Unclear -> send `confirm_line` again.
   - exit 0 -> tell the owner it is on and how to turn it off ("끄고 싶으면
     꺼달라고 말해 주세요" in their words).
     - `unit: not_on_this_os` -> also say plainly the scheduled part does not
       run on this device yet.
     - `then: dogany-computer-use` -> continue with that skill's setup
       (its own onboarding asks the owner again before installing).
   - exit 3 -> the switch is on but the background unit failed
     (`unit_detail`). Tell the owner; do not retry another way.
- Never pass `--confirm` with words the owner did not send in this turn.

## Turn off (owner asked)
- `preview-switch.py off <ID>` -> exit 0. No confirm (safe, reversible).
  Tell the owner it is off and that asking again turns it back on.
- exit 3 -> off in conf, background unit not unloaded: report it.

## Query
- "what previews are there" / "is X on" -> `preview-switch.py list` or
  `status <ID>`. Answer in plain words. `source: default` = never switched.

## Never
- Never edit `config/agent.conf` by hand for these keys; never ask the
  owner to edit it.
- Never turn a preview on without the owner asking in this conversation.
