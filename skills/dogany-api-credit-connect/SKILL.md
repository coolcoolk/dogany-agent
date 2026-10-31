---
name: dogany-api-credit-connect
display_name: API 크레딧 연결 안내
scope: framework
layer: shipped
description: Guides the owner, one step per message, through connecting the plan's monthly API credit so background (helper) work runs on it instead of the chat limit -- make a key in the Claude Console, reach the Mac terminal (at the Mac, or over SSH from a remote app such as Tailscale + Termius, keychain unlock included), store the key in the keychain (Linux: a chmod 600 key file), then verify. Action triggers: "API 크레딧 연결해줘", "크레딧 연결 어떻게 해?", "API 키 등록", "API 크레딧 온보딩", "connect my API credit", "set up the API key for background work". Query triggers: "API 크레딧이 뭐야?", "백그라운드 작업이 왜 크레딧을 써?", "크레딧 연결됐어?", "API 키 잘 들어갔어?", "is my API credit connected?", "why would helpers use API credit?". Offer trigger: once per instance, when PLAN is a Max or Team plan and no key is stored (check.sh offer-due says due). Outputs: step-by-step guidance in the owner's language; verify verdict (key present + one tiny test call). Side effects: NONE on the key -- the agent never sees, echoes, logs or stores it; the owner types it into the keychain prompt themselves. offer-due writes one marker file so the offer is made once. Stopgap until the v2.7 chat secret intake (dec-347). Trigger tier: BEST-EFFORT.
---

# dogany-api-credit-connect -- API credit connection guide (stopgap)

Reader: agent. Stopgap until v2.7 chat secret intake (dec-347: one
secret-store tool + key taken through chat). Then this skill retires.

Plan monthly API credit (max_5x $100, max_20x $200, team_* per seat; owner
config PLAN=) pays for dispatch runtime `claude-api` (DGN-1963). Dispatcher
reads key via `api_key_read` in routines/dispatch-detached.sh:
- macOS: keychain service `dogany-anthropic-api-credit`, account `dogany`.
- else: file named by env ANTHROPIC_API_KEY_FILE (bridge loads it from
  `<instance>/.telegram_bot/.env`).
No key -> lane off, helpers stay on subscription. Nothing breaks.

## HARD RULES -- the key

- Key NEVER enters chat. Owner pastes it to chat anyway -> do not repeat,
  quote, store or use it; tell owner to delete that message and revoke that
  key in the Console, then make a new one. (copy: step 2 leak line)
- Agent never runs a command that reads the secret into output. Verify only
  through `check.sh` (prints verdict words, never the key).
- Never write the key to memory, worklog, ticket, files/, logs, argv.
- Agent never types the key for the owner. Owner types it into the hidden
  prompt themselves.

## trigger tier

BEST-EFFORT. Description auto-fire. Offer-once rides `check.sh offer-due`
(atomic check + record); no hook calls it yet -- guaranteed wiring comes
with v2.7 intake.

## flow

| id | step | lane | kind | after |
|----|------|------|------|-------|
| s0 | offer once (proactive path only) | rule | value | - |
| s1 | what it is for | llm | - | s0 |
| s2 | make key in Console | llm | - | s1 |
| s3 | reach Mac terminal | llm | - | s2 |
| s4 | store key | llm | - | s3 |
| s5a | verify presence | rule | verify | s4 |
| s5b | verify one tiny call | rule | verify | s5a |
| s6 | report | llm | - | s5b |

Delivery rules (all steps):
- ONE step per message. Wait for owner reply ("했어" / done / question)
  before next step. Owner stuck -> help inside same step.
- Speak owner's configured language. Copy below = Korean source of truth;
  other language -> translate meaning, same structure.
- Commands go ONLY in fenced code blocks, sent as their own block. Never
  inline a command in a sentence. Owner copies block as-is.
- Owner copy confirmed 2026-10-09 (dec-348).
- Never say internal names (script, function, lane, dispatcher) to owner.
  Say "백그라운드 작업" / "도우미 작업".

### s0 offer once (proactive path)

Owner asked -> skip to s1. Otherwise, at a natural pause after install:

```
DOGANY_ROOT=__PROJECT_ROOT__ __PROJECT_ROOT__/.claude/skills/dogany-api-credit-connect/check.sh offer-due
```

`due` -> send offer once. `not_due ...` -> say nothing, never offer again.

<!-- OWNER-SAY -->
> 요금제에 매달 쓸 수 있는 API 크레딧이 들어 있어요. 연결해 두면 백그라운드 작업이 대화 한도 대신 이 크레딧을 써요. 5분 정도 걸리는데, 지금 같이 연결해 볼까요?
<!-- /OWNER-SAY -->

No / later -> drop it. Do not ask again (marker already written).

### s1 what it is for (one line + spend note)

<!-- OWNER-SAY -->
> 백그라운드 작업(도우미 작업)이 대화 한도를 쓰지 않고 요금제의 월 API 크레딧을 쓰게 하는 연결이에요. 크레딧은 매달 새로 들어오고, 남은 건 다음 달로 넘어가지 않아요.
>
> 크레딧은 도우미 작업에만 쓰여요. 제가 일을 따로 맡기는 경우가 적으면 쓰는 양도 적어요.
<!-- /OWNER-SAY -->

### s2 make key in Console

<!-- OWNER-SAY -->
> 1단계: 클로드 콘솔에 요금제 계정으로 로그인해서 키 메뉴에서 새 키를 만들어 주세요. 이름은 아무거나 괜찮아요.
>
> 키는 만들 때 한 번만 보여요. 메모장 같은 곳에 잠깐 복사해 두세요. 이 대화창에는 절대 붙여넣지 마세요. 키를 넣는 곳은 다음 단계에서 따로 알려 드릴게요.
<!-- /OWNER-SAY -->

Console link (send as plain link line):

```
https://console.anthropic.com/settings/keys
```

Leak line (owner pasted key into chat):

<!-- OWNER-SAY -->
> 방금 보내 주신 키는 대화에 남았으니 쓰지 않을게요. 그 메시지는 지워 주시고, 콘솔에서 그 키를 삭제한 뒤 새 키를 하나 더 만들어 주세요.
<!-- /OWNER-SAY -->

### s3 reach Mac terminal

Ask where owner is, then send matching branch.

At the Mac:

<!-- OWNER-SAY -->
> 2단계: 맥에서 터미널 앱을 열어 주세요. 스포트라이트(⌘ + 스페이스)에서 "터미널"을 치면 나와요. 열었으면 알려 주세요.
<!-- /OWNER-SAY -->

Away from the Mac (remote SSH app; examples Tailscale + Termius):

<!-- OWNER-SAY -->
> 2단계: 밖에서도 휴대폰의 원격 접속 앱으로 맥 터미널에 들어갈 수 있어요. 예를 들어 테일스케일로 맥과 휴대폰을 연결하고, 터미어스 같은 앱으로 맥에 접속하면 돼요. 접속했으면, 원격 접속에서는 키 보관함이 잠겨 있어서 먼저 아래 명령으로 열어 주세요. 맥 로그인 비밀번호를 물어봐요.
<!-- /OWNER-SAY -->

```
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

Owner has no remote access set up yet -> offer "맥 앞에 계실 때 하자" or
walk Tailscale + Termius install; do not invent other paths.

### s4 store key

macOS (both branches of s3):

<!-- OWNER-SAY -->
> 3단계: 아래 명령을 붙여넣고 엔터를 누르면 비밀번호를 묻는 칸이 나와요. 거기에 1단계에서 복사한 키를 붙여넣고 엔터를 두 번(입력, 확인) 눌러 주세요. 입력하는 동안 글자는 화면에 보이지 않는 게 정상이에요. 키는 맥의 키 보관함에만 저장되고 저는 보지 못해요.
<!-- /OWNER-SAY -->

```
security add-generic-password -s dogany-anthropic-api-credit -a dogany -w
```

Owner says "already exists" error (key stored before, replacing) -> send
the update form (`-U`), same prompt copy:

```
security add-generic-password -U -s dogany-anthropic-api-credit -a dogany -w
```

Linux host (no keychain): key file + env line. Dispatcher reads env
ANTHROPIC_API_KEY_FILE (api_key_read, non-macOS branch); bridge loads it
from `.telegram_bot/.env`. Same s4 prompt copy, then:

```
mkdir -p ~/.config/dogany && ( umask 077; read -rs -p "API key: " k; echo; printf '%s\n' "$k" > ~/.config/dogany/anthropic-api-credit.key; unset k ) && chmod 600 ~/.config/dogany/anthropic-api-credit.key
```

```
echo "ANTHROPIC_API_KEY_FILE=$HOME/.config/dogany/anthropic-api-credit.key" >> __PROJECT_ROOT__/.telegram_bot/.env
```

Env line takes effect at next bridge start. Bridge restart = owner-gated
(rules/hot.framework.md Machine safety); offer it, never auto-restart.

After owner says done -> s5. Then tell owner they can delete the key from
their notepad copy.

### s5 verify (agent runs, owner waits)

s5a presence (never reads secret on macOS):

```
DOGANY_ROOT=__PROJECT_ROOT__ __PROJECT_ROOT__/.claude/skills/dogany-api-credit-connect/check.sh present
```

s5b one tiny call: key from the dispatcher's own key read, piped to
probe.py (model list, then a 1-token message on the cheapest listed model;
cost ~ fractions of a cent). Proves key path + key accepted + credit can
pay:

```
DOGANY_ROOT=__PROJECT_ROOT__ __PROJECT_ROOT__/.claude/skills/dogany-api-credit-connect/check.sh probe
```

macOS: first keychain read by the agent's process may raise an approval
popup on the Mac screen -> tell owner to click "항상 허용" (the dispatcher
reads the same item later, so allow it once now). Probe stalls on that
popup with nobody at the Mac -> say so, retry when owner is at the Mac.

Verdict -> next step (s6 copy):

| verdict | meaning | next step |
|---------|---------|-----------|
| ok | key stored + accepted | success copy |
| missing | nothing at keychain item / key file | redo s4 (check same Mac user account) |
| env_unset | Linux: env line absent | redo s4 env line, then restart offer |
| key_rejected | API refused the key | key typo or revoked -> new key (s2), store with `-U` |
| credit_low | key fine, credit balance empty | check Console billing / plan link; nothing to redo |
| failed | probe could not finish (network, API error) | key may be fine; retry once later, then report detail line upstream (dogany-upstream-report) |

### s6 report

<!-- OWNER-SAY -->
> 연결됐어요. 이제 백그라운드 작업은 이번 달 크레딧이 남아 있는 동안 크레딧으로 돌아가요. 메모장에 복사해 둔 키는 지워 주세요.
<!-- /OWNER-SAY -->

Failure: one line what failed (owner terms) + the one next step from the
table. No command names, no verdict words.

<!-- OWNER-SAY -->
> 키가 아직 확인되지 않았어요. 3단계를 한 번만 다시 해 볼까요?
<!-- /OWNER-SAY -->

## bounds

- Framework skill, shipped. Read-only toward the key except the owner's
  own typing; agent never writes keychain or key file itself.
- Never restart bridge without owner yes.
- Admin API key (optional ledger reconcile) out of scope here.
- Retire when v2.7 chat secret intake (dec-347) lands; update/remove in the
  same release.

## model routing

No subagent. Main session guides (conversation). check.sh = shell;
probe.py = stdlib HTTP, one 1-token call.
