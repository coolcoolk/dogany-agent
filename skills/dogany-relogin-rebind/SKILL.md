---
name: dogany-relogin-rebind
display_name: 재로그인 절차 안내
description: Guides the owner through the correct re-login procedure (`claude auth login` in a plain terminal, no env vars) after SSH re-login or account switch, and offers a bridge restart when the owner wants the switch to take effect immediately rather than waiting for self-heal. Action triggers: "로그인 다시 했어", "재로그인했어", "계정 바꿨어", "claude 계정 새로 로그인", "토큰 리바인드 해줘", "re-logged into Claude", "switched Claude account", "SSH로 클로드 재로그인", "claude 다시 로그인했어". Symptom triggers: "한도가 옛 계정 걸로 나와", "세션이 옛 계정을 물고 있어", "usage가 다른 계정으로 나와", session hitting old account limit after re-login, usage shows wrong account, bridge still bound to previous account. Query triggers: "토큰 동기화 상태 확인", "자격증명 일치해?", "check credential sync", "keychain token 맞아?", "credentials 싱크 확인해줘" -- answered with procedure confirmation + optional usage-window check, NOT a FILE/KEYCHAIN verdict (that comparison is retired, see body). Outputs: procedure confirmation, optional usage-window account check, bridge restart offer. Side effects: NONE on keychain or credentials file -- this skill is read-only and NEVER writes either store; bridge restart only after owner confirm; restart completion notice pushed via self_restart.sh. Platform: guidance applies on any platform (`claude auth login` is a CLI command, not keychain-specific); this skill no longer invokes token-sync.sh's sync subcommand at all. Trigger tier: BEST-EFFORT.
---

# dogany-relogin-rebind -- account re-login guidance

Claude CLI keeps ONE authoritative credential copy. Keychain is primary;
~/.claude/.credentials.json is fallback only. A successful keychain write does
NOT update the file. So after any runtime token rotation, FILE != KEYCHAIN is
the NORMAL state, not a defect.

The CLI already handles concurrent access safely by itself: cross-process file
lock on refresh, re-read-after-lock (adopts a peer's token instead of hitting
the endpoint again), compare-and-swap save (never overwrites a newer peer
write), and 401-recovery-from-disk (silently adopts a fresh token on API 401).
Running bridges self-heal without a restart.

Historical root incident: DGN-393 (misdiagnosed direction). Corrected by
DGN-1050. Framework skill -- canonical template (DGN-591).

## HARD PROHIBITION -- never write the keychain

This skill NEVER runs `token-sync.sh sync` and NEVER writes the keychain any
other way. Read-only, always. Do not add this step back. Do not build a
"smarter" compare-then-overwrite version either.

Why: FILE is stale by design after any runtime token rotation (CLI writes
keychain only, not file, on refresh). Unconditional FILE->KEYCHAIN overwrite
re-injects a superseded refresh token. Reuse of a superseded token triggers
server-side token-family revocation -- kills ALL estate instances sharing the
account, at once. Confirmed root cause of repeated estate-wide outages
(DGN-1050, 16 restarts since 07-29, 4 in one day). No freshness-check fixes
this: the file side of any comparison is structurally always behind after
rotation, so no compare-then-overwrite variant is ever safe.

## platform

`claude auth login` is a CLI command, not keychain-specific -- this guidance
applies on any platform the owner runs it on. The old macOS-vs-Linux keychain
divergence problem no longer applies to this skill's flow, because this skill
does not touch either store anymore. token-sync.sh itself still carries its
own `uname -s` guard (script-side, unchanged, out of this skill's scope), but
this skill's flow does not call any subcommand of it.

## trigger tier

BEST-EFFORT. Description auto-fire. No hook required.

## correct procedure (what to tell the owner)

Owner re-logs in or switches account with:

`claude auth login`

Run in a plain terminal, NO env vars set. This writes through the CLI's
composite store correctly. On account switch, it also revokes the previous
account's refresh token server-side.

Do not use the old SSH-relogin-writes-file-only procedure -- that is what
created the permanent FILE/KEYCHAIN mismatch in the first place (DGN-393),
which this skill used to "fix" in the wrong direction.

## response steps

1. Owner reports re-login / account switch / stale-account symptom -> confirm
   the correct procedure above. If owner has not run `claude auth login` yet,
   tell them to run it now (plain terminal, no env vars).
2. Explain self-heal: running bridges pick up the new account on the next API
   call via 401-recovery-from-disk. No restart is strictly required.
3. If owner wants the switch to take effect immediately (not wait for next
   natural API call) -> offer bridge restart (below, owner-gated).
4. If owner asks "did it take" / "토큰 맞아?" / "싱크 확인" -- do NOT run a
   FILE-vs-KEYCHAIN comparison (see next section for why). Answer instead:
   `claude auth login`'s own terminal output already confirms the account; if
   the owner wants a live check, offer the usage-window check (post-restart
   verify below).

## why FILE/KEYCHAIN status is retired (read before reviving it)

Under this model, FILE differs from KEYCHAIN after essentially any runtime
token refresh, on any account, correct or wrong. A MISMATCH tells you a
refresh happened -- nothing about which account is bound. It cannot
distinguish "stale copy of the CORRECT account" from "stale copy of the WRONG
account." Presenting MISMATCH as a problem to fix is what caused this
incident. This skill does not surface a MATCH/MISMATCH verdict. If a future
need for account-identity verification arises, build it against something
that actually encodes identity (e.g. usage-window reset time), not the
file/keychain diff.

## bridge restart (owner-gated)

ASK owner first. Provide options list. NEVER auto-restart.

On owner confirm YES:
`__PROJECT_ROOT__/bridge/self_restart.sh --trigger user --reason "account switch re-login (dogany-relogin-rebind)"`

--trigger user required: reached only after explicit owner confirm, so idle
guard must be bypassed (DGN-546); omitting it causes silent deferral
mid-conversation (DGN-591 incident).

self_restart.sh pushes its own completion notice to the agent's Telegram
channel. Do not send a separate "restart done" message -- the notice IS the
confirmation.

## post-restart verify (optional, only if restart was run)

- New bridge process start time must be AFTER the owner's login moment.
- Run `routines/claude-usage.sh` -- window should reflect the new account
  (different reset time / quota from before). This is the one honest live
  signal this skill offers; it reflects real account identity, not a file
  diff.

If window still shows the old account pattern: report anomaly to owner. May
need manual login verification (owner re-runs `claude auth login`, confirms
account in the browser flow).

## report to owner (output rules)

Report in crisp bullets. Follow RULES output rules: no internal paths, no
script names, no command text in user-facing body.

Say: "로그인 절차 안내 완료" not "explained claude auth login".
Say: "재시작 완료 (재시작 알림 도착)" not "self_restart.sh returned".
Never say "동기화됨" / "불일치" (matched/mismatched) about FILE vs KEYCHAIN --
that framing is retired.

## bounds

- Framework skill; canonical template (DGN-591). Read-only: never writes
  keychain, never writes credentials file, in any code path.
- Never auto-restart bridge. Owner confirm required.
- Never invoke `token-sync.sh sync`. Not "with a freshness check," not ever --
  see HARD PROHIBITION above for why no variant is safe.
- Pending verification (DGN-1050): server-side family-revocation-on-reuse is
  inferred from the outage timeline, not a captured error body. Treat as
  confirmed once one 8h access-token-expiry boundary passes with no
  estate-wide death after this skill version lands.

## model routing

No subagent. Main session explains procedure, reports to owner. No script
I/O required for the core flow (procedure guidance is prose). Usage-window
check, if run, uses existing routines/claude-usage.sh output only.
