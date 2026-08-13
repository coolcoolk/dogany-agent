---
name: dogany-relogin-rebind
display_name: 재로그인 토큰 동기화
description: Syncs macOS Keychain Claude credential entry with ~/.claude/.credentials.json after SSH re-login or account switch. Action triggers: "로그인 다시 했어", "재로그인했어", "계정 바꿨어", "claude 계정 새로 로그인", "토큰 리바인드 해줘", "re-logged into Claude", "switched Claude account", "SSH로 클로드 재로그인", "claude 다시 로그인했어". Symptom triggers: "한도가 옛 계정 걸로 나와", "세션이 옛 계정을 물고 있어", "usage가 다른 계정으로 나와", session hitting old account limit after re-login, usage shows wrong account, bridge still bound to previous account. Query triggers: "토큰 동기화 상태 확인", "자격증명 일치해?", "check credential sync", "keychain token 맞아?", "credentials 싱크 확인해줘". Outputs: FILE/KEYCHAIN hash comparison (MATCH or MISMATCH), sync result on mismatch, prompt for bridge restart on confirmed mismatch+sync. Side effects: overwrites keychain entry "Claude Code-credentials" from credentials file on mismatch; bridge restart only after owner confirm; restart completion notice pushed via self_restart.sh. Platform: macOS only -- Linux hosts have no keychain to drift, skill is not-applicable there (script exits 3 without action). Trigger tier: BEST-EFFORT.
---

# dogany-relogin-rebind -- SSH re-login token sync

After SSH re-login, Claude CLI writes new OAuth token to
~/.claude/.credentials.json. macOS Keychain keeps OLD token. Claude CLI
reads Keychain FIRST on macOS -> bridge restarts re-bind old account.
This skill: compare token values, sync Keychain from file, restart (owner-gated).

Root incident: DGN-393. Framework skill -- promoted to canonical template (DGN-591). macOS Keychain-specific; Linux agents: no-op / not-applicable.

## platform (read FIRST)

macOS only. The token-sync.sh script guards on `uname -s`: on any non-Darwin
host it prints a NOT-APPLICABLE line and exits 3 WITHOUT touching credentials or
keychain. On Linux there is no login keychain for Claude CLI to prefer, so no
drift exists to fix. If this skill fires on a Linux agent, report to the owner
that it does not apply on this platform and STOP -- do not attempt any sync or
restart.

## trigger tier

BEST-EFFORT. Description auto-fire. No hook required (optional, not
must-happen integrity op).

## precondition check (step 1 -- ALWAYS first)

Check ~/.claude/.credentials.json mtime. Must be NEWER than owner's
stated re-login moment.

If STALE (mtime older than login): login likely not finished on this host,
or token not yet written. STOP. Tell owner to finish login first (on remote
SSH host), then re-run. Do NOT proceed to sync.

DGN-393 root cause: bridge restarted BEFORE credentials file was refreshed.
Precondition guards against repeat.

## status check (step 2)

Run: `bash <skill-dir>/token-sync.sh status`

<skill-dir> = this skill's own directory. Resolve it from the running agent's
`.claude/skills/dogany-relogin-rebind/` (do NOT hardcode any absolute agent
path -- each instance has its own workspace).

Output:
- credentials file mtime
- FILE token: first 16 chars of sha256 (display only)
- KEYCHAIN token: first 16 chars of sha256 (display only)
- MATCH or MISMATCH (verdict: token value equality; hash compare is fallback only)
- script exits 0 on MATCH, 1 on MISMATCH, 2 on read failure, 3 on non-macOS (not-applicable)

## query-only path (step 3)

If owner asked "상태 확인" / "check sync" / query-only phrasing:
- Report status result (hash prefixes + MATCH/MISMATCH + mtime).
- STOP here. Do not sync. Do not prompt restart.
- NOTE: Claude CLI re-serializes keychain JSON differently (same tokens, different bytes), so hash-only compare false-positives MISMATCH. Verdict comes from token-value equality. Hashes are display only.

## mismatch sync (step 4)

MISMATCH found -> run: `bash <skill-dir>/token-sync.sh sync`

Script overwrites Keychain entry from credentials file, then re-runs status
internally. Must exit 0 (MATCH confirmed) before proceeding.

If sync exits non-zero: report failure, do not proceed to restart.

## bridge restart (step 5 -- owner-gated)

Keychain sync confirmed MATCH. Bridge restart = service op.

ASK owner first. Provide options list. NEVER auto-restart.

On owner confirm YES:
`__PROJECT_ROOT__/bridge/self_restart.sh --trigger user --reason "token rebind after re-login (dogany-relogin-rebind)"`

--trigger user required: step 5 is reached only after explicit owner confirm, so idle guard must be bypassed (DGN-546); omitting it causes silent deferral mid-conversation (DGN-591 incident).

self_restart.sh pushes its own completion notice to the agent's Telegram channel.
Do not send a separate "restart done" message -- the notice IS the confirmation.

## post-restart verify (step 6)

After restart notice arrives:
- New bridge process start time must be AFTER sync timestamp.
- Run `routines/claude-usage.sh` -- window should reflect new account
  (different reset time / quota from before).

If window still shows old account pattern: report anomaly to owner. May need
manual login verification.

## report to owner (step 7 -- output rules)

Report in crisp bullets. Follow RULES output rules: no internal paths,
no script names, no command text in user-facing body.

Say: "토큰 저장소 동기화 완료" not "sync subcommand exited 0".
Say: "재시작 완료 (재시작 알림 도착)" not "self_restart.sh returned".
Say: "두 저장소 일치" not "FILE sha256 matches KEYCHAIN sha256".

## keychain write safety

Keychain write is reversible: old token is invalid after re-login anyway.
On MATCH: no-op. Never touch Keychain when hashes already match.
Keychain entry name: "Claude Code-credentials" (exact string).

## bounds

- Framework skill; canonical template (DGN-591). macOS Keychain-specific -- Linux agents treat as no-op / not-applicable (script exits 3, non-destructive).
- Never auto-restart bridge. Owner confirm required (step 5).
- Never touch Keychain on MATCH.
- Precondition check is mandatory. Skip = risk of writing stale token.

## model routing

No subagent. Main session reads output, reports to owner. Script does all
I/O. No reasoning needed beyond step judgment.
