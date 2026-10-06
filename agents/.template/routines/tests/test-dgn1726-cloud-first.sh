#!/usr/bin/env bash
# test-lane: fast
# test-dgn1726-cloud-first.sh -- cloud as the FIRST auto-selected runtime.
#
# Owner 2026-09-26 21:39 (cloud into the dispatch machine like codex, first): with
# no --runtime, a cloud-eligible run goes to cloud before the codex/claude-code
# measured selection (header, CLOUD-FIRST). Everything else stays local.
#
# Locks (selection matrix, do_spawn up to the tier gate):
#   eligible read-only --repo              -> cloud, cloud_first_eligible
#   --worktree --worktree-repo --repo-contained -> cloud, converted to
#       --repo <top> --base <resolved base>, no worktree
#   --worktree --worktree-repo --local-why (no --repo-contained) -> local
#   --worktree without --worktree-repo     -> local (live workspace)
#   task text naming launchctl/.dogany     -> local
#   target under ~/.dogany / instance root -> local
#   expired window / CLOUD_FIRST_UNTIL=off -> local
#   live .cloud-exhausted flag             -> local (expired flag -> cloud)
#   no origin / non-GitHub origin          -> local
#   no CLOUD_ENV_ID, existing claude/<label> -> local
#   dirty tree                             -> local, reason printed
#   explicit --runtime / env runtime       -> explicit wins, no cloud-first
#   --repo-contained without --worktree/--repo -> refused
# DGN-1742 forcing points (LOCAL-WHY in the dispatch header):
#   --repo / --worktree-repo with neither --repo-contained nor --local-why
#       -> refused (exit 1); either one -> accepted
#   explicit --runtime claude-code|codex on an eligible run -> refused
#       (exit 2) unless --local-why; on a non-eligible run -> accepted
#   unpushed base commits -> refused "push <base> to origin first" (exit 2)
#       unless --local-why (then local, recorded)
#   --local-why on an eligible auto run -> local; on --runtime cloud or a
#       workspace-only run, or shorter than 10 chars -> refused
#   --runtime cloud and workspace-only runs need neither flag
# plus one end-to-end auto spawn: status.json runtime=cloud with
# selection_reason=cloud_first_eligible.
#
# Offline: bare origin behind a github.com insteadOf rewrite, the transport
# helper is a stub (DOGANY_CLOUD_ROUTINE_HELPER), quota/hook gates stubbed.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1726-cloudfirst.XXXXXX)"
TDIR="$(cd "$TDIR" && pwd -P)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  chflags -R nouappend "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/config" "$WS/product" "$WS/.claude" \
         "$WS/.telegram_bot" "$WS/bridge" "$TDIR/bin" "$TDIR/home"
cp "$WRAPPER" "$WS/routines/"
for f in ledger-update.sh agentlib.sh dispatch-tier-bindings.tsv conf_reader.py \
         dispatch-model-display.tsv model_display.py cloud_routine.py; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
cp "$REPO/bridge/machine_gate.py" "$WS/bridge/"
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "5h window 97% -- deny"
exit 1
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$TDIR/bin/cloud-routine-stub" <<'EOF'
#!/bin/bash
action="$1"; shift
NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in --name) NAME="$2" ;; esac
  shift 2
done
lbl="$(printf '%s' "$NAME" | awk '{print $2}')"
case "$action" in
  start) echo "trigger_id=trg_FAKE_$lbl"; echo "session_id=cse_FAKE_$lbl"
         echo "view=https://claude.ai/code/cse_FAKE_$lbl"; echo "disabled=yes" ;;
  status) echo "status=running"; echo "state=running" ;;
  disable) echo "disabled=yes" ;;
esac
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/cloud-routine-stub"
printf 'AGENT_LANG=en\nCLOUD_ENV_ID=env_TEST01\n' > "$WS/config/agent.conf"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
EOF
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" -c user.email=t@t -c user.name=t add -A >/dev/null
git -C "$WS" -c user.email=t@t -c user.name=t commit -qm seed >/dev/null

ORIGIN="$TDIR/origin.git"
GH_URL="https://github.com/acme/widgets.git"
git init -q --bare -b main "$ORIGIN"
mkclone() {  # <dir> -- a clone of ORIGIN whose origin reads as GitHub
  git clone -q "$ORIGIN" "$1" 2>/dev/null
  git -C "$1" remote set-url origin "$GH_URL"
  git -C "$1" config "url.$ORIGIN.insteadOf" "$GH_URL"
}
SEED="$TDIR/seed"
git clone -q "$ORIGIN" "$SEED" 2>/dev/null
git -C "$SEED" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
git -C "$SEED" push -q origin HEAD:refs/heads/main 2>/dev/null
git -C "$SEED" push -q origin HEAD:refs/heads/claude/taken 2>/dev/null
CLONE="$TDIR/repo"; mkclone "$CLONE"

export PATH="$TDIR/bin:$PATH"
export DOGANY_CLOUD_ROUTINE_HELPER="$TDIR/bin/cloud-routine-stub"
export DOGANY_START_NOTICE_WINDOW=1 DOGANY_CLOUD_POLL_SECS=1 DOGANY_CLOUD_STATUS_SECS=2
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_CLOUD_ENV_ID DOGANY_CLOUD_FIRST_UNTIL DOGANY_DISPATCH_RUNTIME
DD="$WS/routines/dispatch-detached.sh"
printf 'Review the widget parser for off-by-one errors.\n' > "$TDIR/task.md"
printf 'Check the launchctl plist of the bot and reload it.\n' > "$TDIR/task-live.md"
printf 'Read ~/.dogany/agents/x/config/agent.conf and report.\n' > "$TDIR/task-dogany.md"
printf 'Review the .dogany-canonical marker handling.\n' > "$TDIR/task-marker.md"

# select <args...> -- do_spawn up to the tier gate. Prints the cloud-first
# line(s), the runtime selection line and SELECTED=... ; DECISION=<record>.
select_rt() {
  local dec="$TDIR/decisions.$RANDOM$RANDOM"
  DISPATCH_TEST=1 DEC="$dec" bash -c '
source "$0"
DECISION_DIR="$DEC"
print_calibration() { :; }
observe_sandbox_available() { :; }
codex_quota_gate() { CODEX_USED_PERCENT=10; CODEX_QUOTA_NOTE=fixture; return 0; }
codex_hook_gate() { CODEX_HOOK_NOTE=fixture; return 0; }
bottleneck_gate() {
  echo "SELECTED=$RUNTIME REPO=$REPO_PATH WT=$WT_SLUG BASE=${WT_BASE_ARG:-} TIMEOUT=$TIMEOUT_MIN"
  echo "DECISION=$(ls "$DECISION_DIR"/*.json 2>/dev/null | head -1)"
  exit 0
}
do_spawn' "$DD" --task "${TASK:-$TDIR/task.md}" --bottleneck accuracy \
    --bottleneck-why "verifying a given parser change set" --label "${LBL:-cf-probe}" "$@" 2>&1
}
field() {  # <decision json> <key>
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2" 2>/dev/null
}
expect_local() {  # <name> <code> <output>
  local out="$3" dec
  dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
  if grep -q "^SELECTED=codex " <<< "$out" \
     && grep -q "^\[dispatch\] cloud-first: not chosen -- $2" <<< "$out" \
     && [[ "$(field "$dec" selection_reason)" == read_quota_verified || "$(field "$dec" selection_reason)" == write_hooks_and_quota_verified ]] \
     && grep -q "cloud_first=$2" <<< "$(field "$dec" selection_detail)"; then
    ok "$1 -> local (cloud-first not chosen: $2; recorded in selection_detail)"
  else
    bad "$1: expected local/$2"; echo "$out" | sed 's/^/    /'
  fi
}

echo "== eligible =="
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
if grep -q "^SELECTED=cloud REPO=$CLONE WT= BASE=main TIMEOUT=120$" <<< "$out" \
   && grep -q "^\[dispatch\] cloud-first: cloud chosen (read-only; eligible: target=$CLONE base=main until=2026-11-05)$" <<< "$out" \
   && [[ "$(field "$dec" selected_runtime)" == cloud && "$(field "$dec" selection_reason)" == cloud_first_eligible \
         && "$(field "$dec" requested_runtime)" == auto ]]; then
  ok "read-only + --repo (GitHub, pushed, clean) -> cloud, cloud_first_eligible, cloud default timeout"
else bad "eligible read-only"; echo "$out" | sed 's/^/    /'; fi
grep -q "model auto-resolved\|SELECTED=cloud" <<< "$out" && ! grep -q "codex quota" <<< "$out" \
  && ok "cloud chosen before the codex quota probe" || bad "codex probed before cloud"

out="$(select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained)"
if grep -q "^SELECTED=cloud REPO=$CLONE WT= BASE=main " <<< "$out" \
   && grep -q "worktree 'cf' becomes a cloud run -- result branch claude/cf-probe on origin" <<< "$out" \
   && grep -q "cloud chosen (worktree(repo-contained);" <<< "$out"; then
  ok "--worktree --worktree-repo --repo-contained -> cloud, converted to --repo/--base"
else bad "worktree repo-contained"; echo "$out" | sed 's/^/    /'; fi

out="$(TASK="$TDIR/task-marker.md" select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "'.dogany-canonical' in task text is not a live path" \
  || { bad "marker text"; echo "$out" | sed 's/^/    /'; }

echo "== live / work kind -> local =="
expect_local "--worktree --worktree-repo --local-why, no --repo-contained" worktree_not_repo_contained \
  "$(select_rt --worktree cf --worktree-repo "$CLONE" --local-why "build needs the local toolchain")"
expect_local "--worktree cut from the workspace" live_instance_target \
  "$(select_rt --worktree cf --repo-contained)"
expect_local "task names launchctl" task_touches_live "$(TASK="$TDIR/task-live.md" select_rt --read-only --repo-contained --repo "$CLONE")"
expect_local "task names ~/.dogany" task_touches_live "$(TASK="$TDIR/task-dogany.md" select_rt --read-only --repo-contained --repo "$CLONE")"
printf 'Inspect %s/routines for dead code.\n' "$WS" > "$TDIR/task-ws.md"
expect_local "task names the workspace path" task_touches_live "$(TASK="$TDIR/task-ws.md" select_rt --read-only --repo-contained --repo "$CLONE")"
mkdir -p "$TDIR/home/.dogany/agents"; mkclone "$TDIR/home/.dogany/agents/inst"
expect_local "target under ~/.dogany" live_instance_target \
  "$(HOME="$TDIR/home" select_rt --read-only --repo-contained --repo "$TDIR/home/.dogany/agents/inst")"
INST="$TDIR/inst-root"; mkclone "$INST"; mkdir -p "$INST/config"; : > "$INST/config/agent.conf"
expect_local "target is an instance root (config/agent.conf)" live_instance_target \
  "$(select_rt --read-only --repo-contained --repo "$INST")"
expect_local "read-only without --repo" no_repo_target "$(select_rt --read-only)"

echo "== window / credit -> local =="
expect_local "expired window" cloud_first_expired "$(DOGANY_CLOUD_FIRST_UNTIL=2020-01-01 select_rt --read-only --repo-contained --repo "$CLONE")"
expect_local "CLOUD_FIRST_UNTIL=off" cloud_first_off "$(DOGANY_CLOUD_FIRST_UNTIL=off select_rt --read-only --repo-contained --repo "$CLONE")"
cp "$WS/config/agent.conf" "$TDIR/agent.conf.bak"
printf 'CLOUD_FIRST_UNTIL=2020-01-01\n' >> "$WS/config/agent.conf"
expect_local "expired window from agent.conf" cloud_first_expired "$(select_rt --read-only --repo-contained --repo "$CLONE")"
cp "$TDIR/agent.conf.bak" "$WS/config/agent.conf"
printf '{"expiry": "2099-01-01T00:00:00+00:00"}' > "$WS/.claude/.cloud-exhausted"
expect_local "live .cloud-exhausted flag" cloud_credit_exhausted "$(select_rt --read-only --repo-contained --repo "$CLONE")"
printf '{"expiry": "2020-01-01T00:00:00+00:00"}' > "$WS/.claude/.cloud-exhausted"
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "expired .cloud-exhausted flag -> cloud again" || { bad "expired flag"; echo "$out" | sed 's/^/    /'; }
rm -f "$WS/.claude/.cloud-exhausted"

echo "== cloud not runnable / not the same code -> local =="
NOREMOTE="$TDIR/noremote"; git init -q -b main "$NOREMOTE"
git -C "$NOREMOTE" -c user.email=t@t -c user.name=t commit -q --allow-empty -m x
expect_local "no origin" no_github_remote "$(select_rt --read-only --repo-contained --repo "$NOREMOTE")"
NONGH="$TDIR/nongh"; git clone -q "$ORIGIN" "$NONGH" 2>/dev/null
expect_local "non-GitHub origin" no_github_remote "$(select_rt --read-only --repo-contained --repo "$NONGH")"
printf 'AGENT_LANG=en\n' > "$WS/config/agent.conf"
expect_local "no CLOUD_ENV_ID" cloud_env_unconfigured "$(select_rt --read-only --repo-contained --repo "$CLONE")"
cp "$TDIR/agent.conf.bak" "$WS/config/agent.conf"
expect_local "claude/<label> already on origin" result_branch_exists "$(LBL=taken select_rt --read-only --repo-contained --repo "$CLONE")"
AHEAD="$TDIR/ahead"; mkclone "$AHEAD"
git -C "$AHEAD" -c user.email=t@t -c user.name=t commit -q --allow-empty -m local1
git -C "$AHEAD" -c user.email=t@t -c user.name=t commit -q --allow-empty -m local2
# DGN-1742: unpushed base commits no longer fall back to local silently.
expect_refused_unpushed() {  # <name> <output> <rc>
  if [[ $3 -eq 2 ]] && grep -q '^\[dispatch\] REFUSED: cloud-first blocked by unpushed commits (main is 2 commit(s) ahead of origin.*) -- push main to origin first, or pass --local-why "<reason, >=10 chars>" to run local\.$' <<< "$2" \
     && ! grep -q "^SELECTED=" <<< "$2"; then
    ok "$1 -> refused (exit 2): push main to origin first"
  else
    bad "$1: expected unpushed refusal (rc=$3)"; echo "$2" | sed 's/^/    /'
  fi
}
out="$(select_rt --read-only --repo-contained --repo "$AHEAD")"; rc=$?
expect_refused_unpushed "unpushed base commits" "$out" $rc
out="$(select_rt --worktree cf --worktree-repo "$AHEAD" --repo-contained)"; rc=$?
expect_refused_unpushed "unpushed base commits (worktree)" "$out" $rc
git -C "$AHEAD" branch -q feat origin/main
out="$(select_rt --worktree cf --worktree-repo "$AHEAD" --repo-contained --base main)"; rc=$?
expect_refused_unpushed "explicit --base with unpushed commits" "$out" $rc
out="$(select_rt --read-only --repo-contained --repo "$AHEAD" --local-why "base carries a private unpushed fix")"
expect_local "unpushed base commits + --local-why" unpushed_commits "$out"
grep -q "main is 2 commit(s) ahead of origin" <<< "$out" && ok "unpushed + --local-why: the count is printed" || bad "no ahead count"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
grep -q "local_why given; .*local_why=base carries a private unpushed fix" <<< "$(field "$dec" selection_detail)" \
  && ok "unpushed + --local-why: reason recorded in selection_detail" || { bad "unpushed local_why record"; field "$dec" selection_detail; }
out="$(select_rt --worktree cf --worktree-repo "$AHEAD" --local-why "base carries a private unpushed fix")"
expect_local "unpushed (worktree) + --local-why, not repo-contained" worktree_not_repo_contained "$out"
DIRTY="$TDIR/dirty"; mkclone "$DIRTY"
printf 'x\n' > "$DIRTY/f"; git -C "$DIRTY" add f
expect_local "uncommitted tracked changes" uncommitted_changes "$(select_rt --read-only --repo-contained --repo "$DIRTY")"
BEHIND="$TDIR/behind"; mkclone "$BEHIND"
git -C "$SEED" -c user.email=t@t -c user.name=t commit -q --allow-empty -m newer
git -C "$SEED" push -q origin HEAD:refs/heads/main 2>/dev/null
git -C "$BEHIND" fetch -q origin 2>/dev/null
out="$(select_rt --read-only --repo-contained --repo "$BEHIND")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "local base behind origin (nothing unpushed) -> cloud" || { bad "behind"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "behind an origin head never fetched, base inside origin/main -> cloud" || { bad "behind unfetched"; echo "$out" | sed 's/^/    /'; }
git -C "$AHEAD" checkout -q main
out="$(select_rt --read-only --repo-contained --repo "$AHEAD")"; rc=$?
expect_refused_unpushed "ahead of the last fetch, origin head never fetched" "$out" $rc
grep -q "main is 2 commit(s) ahead of origin (last fetch)" <<< "$out" && ok "unfetched origin: count against origin/main" || bad "no last-fetch count"
out="$(select_rt --read-only --repo-contained --repo "$AHEAD" --local-why "base carries a private unpushed fix")"
expect_local "ahead of the last fetch + --local-why" unpushed_commits "$out"

echo "== explicit runtime wins (with a stated reason, DGN-1742) =="
out="$(select_rt --read-only --repo-contained --repo "$CLONE" --runtime claude-code)"; rc=$?
if [[ $rc -eq 2 ]] && grep -q '^\[dispatch\] REFUSED: --runtime claude-code (source=flag) on a cloud-eligible run (eligible: target=.* base=main until=2026-11-05) needs --local-why "<reason, >=10 chars>" -- or drop the runtime and let cloud-first take it\.$' <<< "$out" \
   && ! grep -q "^SELECTED=" <<< "$out"; then
  ok "--runtime claude-code on an eligible run, no --local-why -> refused (exit 2)"
else bad "explicit claude refusal rc=$rc"; echo "$out" | sed 's/^/    /'; fi
out="$(select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained --runtime codex)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "REFUSED: --runtime codex (source=flag) on a cloud-eligible run" <<< "$out" \
  && ok "--runtime codex on an eligible repo-contained worktree run -> refused" || { bad "explicit codex worktree rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo-contained --repo "$CLONE" --runtime claude-code --local-why "needs the local MCP credentials")"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
grep -q "^SELECTED=claude-code " <<< "$out" && grep -q "cloud-first: not chosen -- explicit_runtime: claude-code (source=flag)" <<< "$out" \
  && grep -q "skipped by explicit runtime claude-code on a cloud-eligible run -- local-why: needs the local MCP credentials" <<< "$out" \
  && [[ "$(field "$dec" selection_reason)" == explicit_runtime ]] \
  && grep -q "local_why=needs the local MCP credentials" <<< "$(field "$dec" selection_detail)" \
  && ok "--runtime claude-code + --local-why on an eligible run -> claude-code, reason recorded" || { bad "explicit claude + local-why"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo-contained --repo "$NONGH" --runtime claude-code)"
grep -q "^SELECTED=claude-code " <<< "$out" && ! grep -q "REFUSED" <<< "$out" \
  && ok "--runtime claude-code on a NON-eligible run needs no --local-why" || { bad "explicit non-eligible"; echo "$out" | sed 's/^/    /'; }
out="$(DOGANY_DISPATCH_RUNTIME=codex select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
[[ $rc -eq 2 ]] && grep -q "REFUSED: --runtime codex (source=env) on a cloud-eligible run" <<< "$out" \
  && ok "DOGANY_DISPATCH_RUNTIME=codex on an eligible run, no --local-why -> refused" || { bad "env runtime refusal rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(DOGANY_DISPATCH_RUNTIME=codex select_rt --read-only --repo-contained --repo "$CLONE" --local-why "codex lane is under test here")"
grep -q "^SELECTED=codex " <<< "$out" && grep -q "explicit_runtime: codex (source=env)" <<< "$out" \
  && ok "DOGANY_DISPATCH_RUNTIME=codex + --local-why -> codex" || { bad "env runtime"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --repo "$CLONE" --runtime cloud)"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
grep -q "^SELECTED=cloud " <<< "$out" && [[ "$(field "$dec" selection_reason)" == explicit_runtime ]] \
  && ok "--runtime cloud still works (explicit_runtime)" || { bad "explicit cloud"; echo "$out" | sed 's/^/    /'; }

echo "== refusals =="
out="$(select_rt --read-only --repo-contained)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "\-\-repo-contained requires --worktree <slug> or --repo <path>" <<< "$out" \
  && ok "--repo-contained without --worktree/--repo -> refused" || bad "repo-contained refusal rc=$rc: $out"

echo "== DGN-1742: a repo run states --repo-contained or --local-why =="
MSG2='^\[dispatch\] REFUSED: a --repo/--worktree-repo run must carry --repo-contained (the work needs only that repo: cloud-eligible) or --local-why "<reason, >=10 chars>" (why it must run local)\.$'
for args in "--read-only --repo $CLONE" "--worktree cf --worktree-repo $CLONE" \
            "--read-only --repo $CLONE --runtime claude-code" "--worktree cf --worktree-repo $CLONE --runtime codex"; do
  # shellcheck disable=SC2086
  out="$(select_rt $args)"; rc=$?
  [[ $rc -eq 1 ]] && grep -q "$MSG2" <<< "$out" && ! grep -q "^SELECTED=" <<< "$out" \
    && ok "neither flag ($args) -> refused (exit 1), message names both options" || { bad "rule-2 refusal ($args) rc=$rc"; echo "$out" | sed 's/^/    /'; }
done
out="$(select_rt --read-only --repo "$CLONE" --local-why "needs the local MCP credentials")"
grep -q "^SELECTED=codex " <<< "$out" \
  && grep -q "cloud-first: not chosen -- local_why: eligible: .* (--local-why: needs the local MCP credentials)" <<< "$out" \
  && ok "--local-why on an eligible auto run -> local, eligibility still recorded" || { bad "local-why eligible auto"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo "$CLONE" --local-why "too short")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "\-\-local-why reason too short" <<< "$out" && ok "--local-why under 10 chars -> refused" || { bad "short local-why rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo "$CLONE" --local-why "a|pipe in the reason")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "invalid --local-why reason" <<< "$out" && ok "--local-why with '|' -> refused" || { bad "pipe local-why rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --repo "$CLONE" --runtime cloud --local-why "needs the local MCP credentials")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "\-\-local-why given but runtime is 'cloud'" <<< "$out" && ok "--local-why with --runtime cloud -> refused" || { bad "cloud local-why rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --local-why "needs the local MCP credentials")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "\-\-local-why given on a workspace-only run" <<< "$out" && ok "--local-why on a workspace-only run -> refused" || { bad "ws local-why rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only)"; rc=$?
grep -q "^SELECTED=" <<< "$out" && ! grep -q "REFUSED" <<< "$out" && ok "workspace-only read-only run needs neither flag" || { bad "ws read-only"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --worktree cf)"; rc=$?
grep -q "^SELECTED=" <<< "$out" && ! grep -q "REFUSED" <<< "$out" && ok "workspace-only worktree run needs neither flag" || { bad "ws worktree"; echo "$out" | sed 's/^/    /'; }

echo "== end to end: auto spawn -> status.json =="
out="$("$DD" --read-only --repo-contained --repo "$CLONE" --task "$TDIR/task.md" --bottleneck accuracy \
  --bottleneck-why "verifying a given parser change set" --label cf-e2e 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q "cloud-first: cloud chosen" <<< "$out" && ok "auto spawn exits 0 on cloud" \
  || { bad "e2e spawn rc=$rc"; echo "$out" | sed 's/^/    /'; }
RD="$(grep -l '^LEDGER_BRANCH=claude/cf-e2e$' "$WS/.telegram_bot/dispatch"/dsp-*/run.env 2>/dev/null | head -1 | xargs dirname 2>/dev/null)"
VM="$TDIR/vm"; git clone -q "$ORIGIN" "$VM" 2>/dev/null
n=0; until grep -q '^CLOUD_SESSION_ID=cse' "$RD/cloud.env" 2>/dev/null || [[ $n -ge 40 ]]; do sleep 0.5; n=$((n+1)); done
git -C "$VM" -c user.email=t@t -c user.name=t commit -q --allow-empty -m result
git -C "$VM" push -q origin HEAD:refs/heads/claude/cf-e2e 2>/dev/null
n=0; until [[ -f "$RD/status.json" || $n -ge 60 ]]; do sleep 0.5; n=$((n+1)); done
if [[ -n "$RD" ]] && python3 - "$RD/status.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["runtime"] == "cloud", d.get("runtime")
assert d["selection_reason"] == "cloud_first_eligible", d.get("selection_reason")
assert d["result"] == "completed", d.get("result")
PY
then ok "status.json: runtime=cloud, selection_reason=cloud_first_eligible, completed"
else bad "status.json ${RD:-no rundir}"; cat "$RD/status.json" 2>/dev/null | head -40; fi

echo
echo "== DGN-1726 cloud-first: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
