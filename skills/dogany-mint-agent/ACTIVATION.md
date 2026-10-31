# mint-agent activation (gate-time procedure, DGN-243 / DGN-1513 SPEC v2)

Module ships DORMANT: home is the canonical repo (`skills/dogany-mint-agent/`), no
instance copy, no symlink, no config touched, weekly-review.sh unpatched.
Zero behavior change until activated. Activation = live change = the owner's
approval.

DGN-1513 SPEC v2: activation now has TWO gates, in order.

DGN-1847 (owner decision dec-242, 2026-10-04): on a MAIN instance both gates
are pre-approved. install.sh seeds gate 1 (DGN-1539) and runs gate 2's
`v2-ctl.sh on` right after the mint (also on a re-install / keep-config
re-run); update.sh runs the same call for a main that has the key but no
active link yet (mains installed before this). Domain/staff instances never
get it, and an owner `v2-ctl.sh off` (config/v2.conf `=off`) is respected by
both. Step 2 below (weekly-review hook) is NOT applied by default: the
product ships no routines/weekly-review.sh, so the module answers owner
requests only and the proactive lane (weekly_eval.py) stays off. update.sh
no longer refreshes this module as a plain 3i skill (it ships ACTIVATION.md
-> activation-gated); an unpinned real copy an older update planted is
archived to .claude/skills-retired/ on the next update.

## gate 1 -- declare the consume root (owner, once)

Add to the crew leader's `.instance.conf` (this key does not exist by
default; a missing key = module stays fully inert, no receipt of any kind):

```
DOGANY_CONSUME_ROOT=<instance>/.dogany-consume
```

`DOGANY_REPO_ROOT` is untouched -- the channel machinery (fetch/archive)
keeps using it exactly as before. `DOGANY_CONSUME_ROOT` names a SEPARATE
`git worktree --detach` of the same repo, advanced by self-update.sh right
after every successful framework update (routines/self-update.sh:
advance_consume_checkout), always to the same tag this instance's channel
just landed. No manual advance step exists or should exist after bootstrap.

## gate 2 -- activate (after gate 1, same session or later)

1. `scripts/v2-ctl.sh on` --
   - bootstraps the consume checkout ONCE, at whatever tag `DOGANY_FW_TAG`
     in `.instance.conf` names (the tag THIS instance just landed -- main
     channel, no tag: the shared checkout's current HEAD instead). No new
     version math: both values already exist before this script runs.
   - appends a `.gitignore` entry for the consume root if this instance is
     itself a git repo (private-continuity-repo pattern, DGN-357) -- the
     checkout carries its own `.git` FILE (worktree), and an unguarded
     `git add -A`/`git add .` on the instance repo would otherwise stage it
     as a phantom gitlink. Idempotent, never rewrites an existing entry.
   - writes `config/v2.conf` (`V2_DOGANY_MINT_AGENT=on`) and creates symlink
     `.claude/skills/dogany-mint-agent -> $DOGANY_CONSUME_ROOT/skills/dogany-mint-agent`.
2. patch `routines/weekly-review.sh` (once; the block self-guards on the
   symlink, so it can stay in place forever). weekly-review.sh is NOT part
   of this module's payload (it ships via the routines/ whole-directory
   channel, unrelated to skills/dogany-mint-agent/) -- adapt the insertion point
   to whatever that file's current structure is; the snippet below is the
   CONTRACT (self-guard, args, --agent-dir), not a byte-exact patch:

   a) after the point where the weekly digest gathers its other sections
      (before the recommendation block is composed into the reply), insert:

   ```bash
   # v2 dogany-mint-agent module (DGN-243): recommendation eval. Inert when the
   # module is off -- the path resolves through .claude/skills/dogany-mint-agent.
   MINT_EVAL="$AGENT_DIR/.claude/skills/dogany-mint-agent/scripts/weekly_eval.py"
   MINT_BLOCK=""
   if [[ -f "$MINT_EVAL" ]]; then
     MINT_ARGS=(--agent-dir "$AGENT_DIR")
     [[ "$DRY" -eq 1 ]] && MINT_ARGS+=(--dry-run)
     MINT_BLOCK="$(/usr/bin/python3 "$MINT_EVAL" "${MINT_ARGS[@]}" \
       2>>"$AGENT_DIR/.telegram_bot/logs/dogany-mint-agent.log" || true)"
     [[ "$DRY" -eq 1 ]] && MINT_BLOCK=""
   fi
   ```

   DGN-1513 w2: `--agent-dir "$AGENT_DIR"` is mandatory here -- without it,
   weekly_eval.py falls back to its own __file__-derived guess, which (w1)
   now fails loud rather than silently guessing wrong, so an unpatched or
   mis-patched hook is a visible ERROR in mint-agent.log, not silent drift.

   b) fold `$MINT_BLOCK` into the composed reply (however that file's
      renderer builds it -- string append, template variable, or an env
      var passed to a renderer script; the OLD, now-superseded shape was a
      literal PROMPT+= append):

   ```bash
   if [[ -n "$MINT_BLOCK" ]]; then
     PROMPT+=$'\n\n'"=== 전담 에이전트 추천 (아래 블록을 문구 수정 없이 회고 메시지 맨 끝에 그대로 포함하라) ==="$'\n'"$MINT_BLOCK"
   fi
   ```

3. verify: `scripts/v2-ctl.sh status` -> conf on / consume tag shown /
   symlink ACTIVE / weekly hook patched. then `weekly_eval.py --dry-run` for
   a decision peek.
4. no bridge restart needed: skills resolve per session; weekly-review.sh is
   read at each cron fire.

## steady state (after bootstrap)

Every subsequent successful `self-update.sh` run advances the consume
checkout to the same tag it just landed the framework at, automatically.
A failed advance is fail-stale: the skill surface stays on its previous
tag, self-update.sh reports a bilingual ERROR line, and the NEXT successful
run retries the same step (routines/self-update.sh:advance_consume_checkout).
`scripts/v2-ctl.sh status` reads the current state
(`git -C $DOGANY_CONSUME_ROOT describe --tags`) -- readable WHILE THE
WORKTREE IS HEALTHY; after a canonical-repo move/re-clone the git side dies
with the rest (files stay, fail-stale) -- see the recovery section below.

## recovery -- canonical repo moved or re-cloned (DGN-1513 F-3)

A worktree's `.git` is a FILE pointing into `<repo>/.git/worktrees/...`, so
the consume checkout survives the canonical repo's death only as files
(fail-stale: content frozen at the last tag, every git operation fails,
including the advance step -- which then reports an ERROR naming this
repair path on every run). It does NOT self-heal. Two modes:

- **repo MOVED** (same clone, new path): run `scripts/v2-ctl.sh on` -- its
  health check detects the broken checkout and runs
  `git worktree repair` automatically (also fix `DOGANY_REPO_ROOT` in
  `.instance.conf` first if the move changed that path).
- **repo RE-CLONED** (old clone deleted): `worktree repair` cannot help --
  the new clone has no `.git/worktrees` admin entry for this checkout.
  `v2-ctl.sh on` says so and prints the recovery steps: inspect the consume
  root for unexpected local changes (there should be none), `rm -rf` it,
  re-run `v2-ctl.sh on` (re-creates the worktree at `DOGANY_FW_TAG`; the
  activation link targets the same path, so it needs no touch-up).

## deactivate (rollback -- one switch)
- `scripts/v2-ctl.sh off` -- removes the symlink. Skill unloads (next
  session), weekly hook self-guards to a no-op. Full v1 behavior restored.
  The consume checkout itself is LEFT IN PLACE (re-activation is then just
  a symlink flip, no re-clone) -- `scripts/v2-ctl.sh on` is symmetric.
- state file `config/minting-state.json` is kept (decline history must
  survive off/on cycles per decision 10).
