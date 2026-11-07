#!/usr/bin/env bash
# test-lane: slow    # measured 34s on a loaded host (2026-10-10; >=10s leaves the push lane, DGN-1734)
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
#   task text naming launchctl/.dogany/workspace + --repo-contained -> refused (DGN-1929, token printed); + --local-why -> local
#   target under ~/.dogany / instance root -> local
#   expired window / CLOUD_FIRST_UNTIL=off -> local
#   no CLOUD_FIRST_UNTIL anywhere          -> local, cloud_first_off (DGN-1912:
#       default off after dec-194; the fixture opts in with a date)
#   live .cloud-exhausted flag             -> local (expired flag -> cloud)
#   no origin / non-GitHub origin          -> local
#   no CLOUD_ENV_ID -> local, cloud absent (DGN-2227); existing claude/<label> -> local
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
# LOSS SCORE (owner 2026-10-09 20:09, no fixed order): the same run moves
#   with the state -- codex passes the burn never reaches, or a codex week
#   near its reset with headroom, beat cloud-first on a repo-contained write
#   run with verified hooks (loss_score_pick, every estimate in the
#   selection line); nothing at risk -> cloud-first (fallback); unverified
#   hooks -> never codex for writes (cloud takes it); cloud at its cap ->
#   vetoed, the run stays local.
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
source "$HARNESS_DIR/fixtures/dispatch-env.sh"
isolate_dispatch_env "$TDIR"
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
bash "$HARNESS_DIR/fixtures/install-dispatch-policy.sh" "$WS"
cp "$REPO/routines/lib/dispatch_storage.py" "$REPO/routines/lib/conf_reader.py" "$WS/routines/lib/"
for f in ledger-update.sh agentlib.sh dispatch-tier-bindings.tsv conf_reader.py cloud_account.py \
         dispatch-model-display.tsv model_display.py run_block.py i18n_lookup.py cloud_routine.py cloud_completion.sh \
         reset_passes.py runtime_loss.py runtime_headroom.py api_credit_pool.py cloud_credit.py; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
mkdir -p "$WS/config" && cp -R "$REPO/config/i18n" "$WS/config/"
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
# DGN-1912: cloud-first is off by default; the fixture instance opts in.
OPTIN=2099-12-31
printf 'AGENT_LANG=en\nCLOUD_ENV_ID=env_TEST01\nCLOUD_FIRST_UNTIL=%s\n' "$OPTIN" > "$WS/config/agent.conf"
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
  # The test lane's push guard (run-tests.sh, DGN-1881) rewrites network push
  # URLs to a dead path; the same rule as pushInsteadOf outranks it by length.
  git -C "$1" config "url.$ORIGIN.pushInsteadOf" "$GH_URL"
}
SEED="$TDIR/seed"
git clone -q "$ORIGIN" "$SEED" 2>/dev/null
git -C "$SEED" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
git -C "$SEED" push -q origin HEAD:refs/heads/main 2>/dev/null
git -C "$SEED" push -q origin HEAD:refs/heads/claude/taken 2>/dev/null
CLONE="$TDIR/repo"; mkclone "$CLONE"

# DGN-2104: owning-login `claude auth status` stub; the cloud is bound to this account.
cat > "$TDIR/bin/claude" <<'CLSTUB'
#!/bin/sh
[ "$1 $2" = "auth status" ] && { echo '{"loggedIn":true,"email":"owner@example.invalid","orgId":"org-owner"}'; exit 0; }
exit 99
CLSTUB
chmod +x "$TDIR/bin/claude"
export DOGANY_CLOUD_ACCOUNT_ID=org-owner
export PATH="$TDIR/bin:$PATH"
export DOGANY_CLOUD_ROUTINE_HELPER="$TDIR/bin/cloud-routine-stub"
export DOGANY_START_NOTICE_WINDOW=1 DOGANY_CLOUD_POLL_SECS=1 DOGANY_CLOUD_STATUS_SECS=2
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_CLOUD_ENV_ID DOGANY_CLOUD_FIRST_UNTIL DOGANY_DISPATCH_RUNTIME
export DOGANY_DISPATCH_MACHINE_SNAPSHOT="0.5 8 16000"   # DGN-2038 I: idle machine, never the host's live load5
# DGN-1975: pin the machine cost term off -- results never depend on the test host.
export DOGANY_DISPATCH_MACHINE_PRESSURE=off
DD="$WS/routines/dispatch-detached.sh"
# DGN-2304: --worktree refuses an inherited cwd repo that differs from the workspace repo.
cd "$WS"
printf 'Review the widget parser for off-by-one errors.\n' > "$TDIR/task.md"
printf 'Run launchctl kickstart gui/501/bot to reload the plist.\n' > "$TDIR/task-live.md"
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
codex_quota_gate() {
  CODEX_USED_PERCENT="${CX_USED:-10}"; CODEX_QUOTA_NOTE=fixture
  CODEX_RESETS_AT=""; [[ -n "${CX_RESET_IN:-}" ]] && CODEX_RESETS_AT=$(( $(date +%s) + CX_RESET_IN ))
  return 0
}
codex_hook_gate() { CODEX_HOOK_NOTE="${HOOK_NOTE:-fixture}"; return "${HOOKS_RC:-0}"; }
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
  # DGN-2293: the owner routing policy keeps portable work off the local codex
  # seat, so the fall-through lands on codex (host-bound) or paced claude-code.
  if grep -qE "^SELECTED=(codex|claude-code) " <<< "$out" \
     && grep -q "^\[dispatch\] cloud-first: not chosen -- $2" <<< "$out" \
     && [[ "$(field "$dec" selection_reason)" =~ ^(read_quota_verified|write_hooks_and_quota_verified|claude_headroom_on_pace|owner_routing_policy)$ ]] \
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
   && grep -q "^\[dispatch\] cloud-first: cloud chosen (read-only; eligible: target=$CLONE base=main until=$OPTIN)$" <<< "$out" \
   && [[ "$(field "$dec" selected_runtime)" == cloud && "$(field "$dec" selection_reason)" == owner_routing_policy \
         && "$(field "$dec" requested_runtime)" == auto ]]; then
  ok "read-only + --repo (GitHub, pushed, clean) -> cloud, cloud_first_eligible, cloud default timeout"
else bad "eligible read-only"; echo "$out" | sed 's/^/    /'; fi
echo "== DGN-2104 cloud account binding =="
set_login() {  # <mode> -- PATH stub for `claude auth status`
  case "$1" in
    owner) body='echo "{\"loggedIn\":true,\"email\":\"owner@example.invalid\",\"orgId\":\"org-owner\"}"; exit 0' ;;
    team)  body='echo "{\"loggedIn\":true,\"email\":\"me@team.invalid\",\"orgId\":\"org-team\"}"; exit 0' ;;
    fail)  body='exit 1' ;;
    hang)  body='sleep 30' ;;
  esac
  printf '#!/bin/sh\n[ "$1 $2" = "auth status" ] && { %s; }\nexit 99\n' "$body" > "$TDIR/bin/claude"
}
set_login team
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
if ! grep -q "^SELECTED=cloud " <<< "$out" && grep -q "cloud_first=cloud_account_unavailable" <<< "$out"; then
  ok "(1) team login, auto -> not cloud, reason cloud_account_unavailable shown"
else bad "team login auto"; echo "$out" | sed 's/^/    /'; fi
set_login fail
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
! grep -q "^SELECTED=cloud " <<< "$out" && grep -q "cloud_account_unavailable: unknown" <<< "$out" \
  && ok "(5) auth status failing -> unknown -> not runnable" || { bad "auth fail"; echo "$out" | sed 's/^/    /'; }
set_login hang
out="$(DOGANY_CLOUD_AUTH_TIMEOUT=1 select_rt --read-only --repo-contained --repo "$CLONE")"
! grep -q "^SELECTED=cloud " <<< "$out" && grep -q "cloud_account_unavailable: unknown" <<< "$out" \
  && ok "(5) auth status timeout -> unknown -> not runnable" || { bad "auth timeout"; echo "$out" | sed 's/^/    /'; }
set_login owner
out="$(DOGANY_CLOUD_ACCOUNT_ID= select_rt --read-only --repo-contained --repo "$CLONE")"
! grep -q "^SELECTED=cloud " <<< "$out" && grep -q "no recorded account id.*CLOUD_ACCOUNT_ID" <<< "$out" \
  && ok "missing recorded id -> unknown + setup hint, never a silent pass" || { bad "missing record"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "(4) owning login -> cloud chosen as before" || { bad "owner"; echo "$out" | sed 's/^/    /'; }
grep -q "^\[dispatch\] runtime selection: cloud (owner_routing_policy; .*loss: " <<< "$out" \
  && ok "nothing at risk -> cloud-first as before, every estimate in the selection line" \
  || { bad "fallback loss line"; echo "$out" | sed 's/^/    /'; }

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
# DGN-1929: a --repo-contained run whose brief names live paths is refused
# with the matched token; --local-why runs local; non-repo-contained is unchanged.
printf 'Inspect %s/routines for dead code.\n' "$WS" > "$TDIR/task-ws.md"
expect_refused_live() {  # <name> <token> <output> <rc>
  # DGN-2293: a live token is no longer a refusal -- the run re-routes to the
  # host-bound seat (local Codex) and the line names the file and the token.
  if [[ $4 -eq 0 ]] && grep -qF "[dispatch] cloud refusal: " <<< "$3" \
     && grep -qF ": $2; re-routing host-bound work to local Codex" <<< "$3" \
     && grep -q "^SELECTED=codex " <<< "$3"; then
    ok "$1 -> re-routed to local Codex naming '$2'"
  else
    bad "$1: expected live-path re-route for '$2' (rc=$4)"; echo "$3" | sed 's/^/    /'
  fi
}
out="$(TASK="$TDIR/task-live.md" select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
expect_refused_live "repo-contained + brief names launchctl" launchctl "$out" $rc
out="$(TASK="$TDIR/task-dogany.md" select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
expect_refused_live "repo-contained + brief names ~/.dogany" .dogany "$out" $rc
out="$(TASK="$TDIR/task-ws.md" select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
expect_refused_live "repo-contained + brief names the workspace path" "$WS" "$out" $rc
out="$(TASK="$TDIR/task-ws.md" select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained)"; rc=$?
expect_refused_live "repo-contained worktree + brief names the workspace path" "$WS" "$out" $rc
expect_local "repo-contained + live brief + --local-why" task_touches_live \
  "$(TASK="$TDIR/task-live.md" select_rt --read-only --repo-contained --repo "$CLONE" --local-why "brief must reload the live bot plist")"
out="$(TASK="$TDIR/task-live.md" select_rt --read-only --repo-contained --repo "$CLONE" --local-why "brief must reload the live bot plist")"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
grep -q "matched: launchctl" <<< "$(field "$dec" selection_detail)" \
  && ok "non-refused live brief: matched token recorded in selection_detail" || bad "token not in selection_detail"
out="$(TASK="$TDIR/task.md" select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "repo-contained + brief without live names -> cloud-first as before" || { bad "clean brief"; echo "$out" | sed 's/^/    /'; }
expect_local "non-repo-contained (--local-why) + live brief" task_touches_live \
  "$(TASK="$TDIR/task-live.md" select_rt --read-only --repo "$CLONE" --local-why "read-only audit of the live plist")"
out="$(TASK="$TDIR/task-live.md" select_rt --read-only --repo "$CLONE" --local-why "read-only audit of the live plist")"
grep -q "task_touches_live:.*(matched: launchctl)" <<< "$out" \
  && ok "non-repo-contained: unchanged local, matched token in the note" || { bad "non-repo-contained note"; echo "$out" | sed 's/^/    /'; }
mkdir -p "$TDIR/home/.dogany/agents"; mkclone "$TDIR/home/.dogany/agents/inst"
expect_local "target under ~/.dogany" live_instance_target \
  "$(HOME="$TDIR/home" select_rt --read-only --repo-contained --repo "$TDIR/home/.dogany/agents/inst")"
INST="$TDIR/inst-root"; mkclone "$INST"; mkdir -p "$INST/config"; : > "$INST/.instance.conf"
expect_local "target is an instance root (.instance.conf, DGN-2293)" live_instance_target \
  "$(select_rt --read-only --repo-contained --repo "$INST")"
expect_local "read-only without --repo" no_repo_target "$(select_rt --read-only)"

echo "== window / credit -> local =="
expect_local "expired window" cloud_first_expired "$(DOGANY_CLOUD_FIRST_UNTIL=2020-01-01 select_rt --read-only --repo-contained --repo "$CLONE")"
expect_local "CLOUD_FIRST_UNTIL=off" cloud_first_off "$(DOGANY_CLOUD_FIRST_UNTIL=off select_rt --read-only --repo-contained --repo "$CLONE")"
cp "$WS/config/agent.conf" "$TDIR/agent.conf.bak"
printf 'AGENT_LANG=en\nCLOUD_ENV_ID=env_TEST01\nCLOUD_FIRST_UNTIL=2020-01-01\n' > "$WS/config/agent.conf"
expect_local "expired window from agent.conf" cloud_first_expired "$(select_rt --read-only --repo-contained --repo "$CLONE")"
printf 'AGENT_LANG=en\nCLOUD_ENV_ID=env_TEST01\n' > "$WS/config/agent.conf"
expect_local "no CLOUD_FIRST_UNTIL (default off, DGN-1912)" "cloud_first_off: CLOUD_FIRST_UNTIL unset" \
  "$(select_rt --read-only --repo-contained --repo "$CLONE")"
out="$(DOGANY_CLOUD_FIRST_UNTIL="$OPTIN" select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "no conf key + env DOGANY_CLOUD_FIRST_UNTIL=<date> opts in -> cloud" \
  || { bad "env opt-in"; echo "$out" | sed 's/^/    /'; }
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
printf 'AGENT_LANG=en\nCLOUD_FIRST_UNTIL=%s\n' "$OPTIN" > "$WS/config/agent.conf"
# DGN-2227: no CLOUD_ENV_ID -> cloud is absent, not a recorded refusal note.
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
# (the fixture path itself reads "cloudfirst", so match the selection words)
if grep -qE "^SELECTED=(codex|claude-code) " <<< "$out" && ! grep -qE 'cloud-first|cloud_first|cloud_env|cloud [0-9n]' <<< "$out" \
   && ! grep -qE 'cloud_first|cloud_env|cloud [0-9n]' <<< "$(field "$dec" selection_detail)"; then
  ok "no CLOUD_ENV_ID -> local, cloud absent (no cloud-first line, no cloud_first note; DGN-2227)"
else bad "no CLOUD_ENV_ID: expected local with cloud absent"; echo "$out" | sed 's/^/    /'; fi
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
out="$(select_rt --read-only --repo-contained --repo "$AHEAD" --local-why "reloads the live instance launchd job after the build")"
expect_local "unpushed base commits + --local-why" unpushed_commits "$out"
grep -q "main is 2 commit(s) ahead of origin" <<< "$out" && ok "unpushed + --local-why: the count is printed" || bad "no ahead count"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
grep -q "local_why given; .*local_why=reloads the live instance launchd job after the build" <<< "$(field "$dec" selection_detail)" \
  && ok "unpushed + --local-why: reason recorded in selection_detail" || { bad "unpushed local_why record"; field "$dec" selection_detail; }
out="$(select_rt --worktree cf --worktree-repo "$AHEAD" --local-why "reloads the live instance launchd job after the build")"
expect_local "unpushed (worktree) + --local-why, not repo-contained" worktree_not_repo_contained "$out"
echo "== DGN-2058: base not on origin -> the dispatcher pushes it (private dsp/* ref) =="
PRIV="$TDIR/priv"; mkclone "$PRIV"
git -C "$PRIV" config dogany.privateRemote 'https://github.com/acme/*'
git -C "$PRIV" -c user.email=t@t -c user.name=t commit -q --allow-empty -m priv1
PRIV_SHA="$(git -C "$PRIV" rev-parse HEAD)"
out="$(LBL=cf-push select_rt --read-only --repo-contained --repo "$PRIV")"; rc=$?
if [[ $rc -eq 0 ]] && grep -q "^SELECTED=cloud REPO=$PRIV WT= BASE=dsp/base-cf-push " <<< "$out" \
   && grep -q "pushing ${PRIV_SHA:0:12} to origin dsp/base-cf-push (private work ref, DGN-2058)" <<< "$out" \
   && grep -q "cloud chosen (read-only; eligible: target=$PRIV base=dsp/base-cf-push until=$OPTIN; auto_pushed: unpushed_commits main -> dsp/base-cf-push)" <<< "$out" \
   && [[ "$(git --git-dir="$ORIGIN" rev-parse -q --verify refs/heads/dsp/base-cf-push)" == "$PRIV_SHA" ]] \
   && [[ "$(git --git-dir="$ORIGIN" rev-parse refs/heads/main)" != "$PRIV_SHA" ]]; then
  ok "unpushed base on a private origin -> pushed as dsp/base-<label> (main untouched) -> cloud on that ref"
else bad "auto-push unpushed (rc=$rc)"; echo "$out" | sed 's/^/    /'; fi
git -C "$PRIV" branch -q dsp/stem HEAD
out="$(LBL=cf-push2 select_rt --worktree cf --worktree-repo "$PRIV" --repo-contained --base dsp/stem)"; rc=$?
if [[ $rc -eq 0 ]] && grep -q "^SELECTED=cloud REPO=$PRIV WT= BASE=dsp/stem " <<< "$out" \
   && grep -q "auto_pushed: base_not_on_origin dsp/stem -> dsp/stem" <<< "$out" \
   && [[ "$(git --git-dir="$ORIGIN" rev-parse -q --verify refs/heads/dsp/stem)" == "$PRIV_SHA" ]]; then
  ok "base not on origin (dsp/* stem) -> pushed under its own name -> cloud"
else bad "auto-push base_not_on_origin (rc=$rc)"; echo "$out" | sed 's/^/    /'; fi
git -C "$AHEAD" branch -q dsp/nopush HEAD
out="$(select_rt --worktree cf --worktree-repo "$AHEAD" --repo-contained --base dsp/nopush)"; rc=$?
if [[ $rc -eq 2 ]] && grep -q "REFUSED: cloud-first blocked because base dsp/nopush is not on origin and the dispatcher could not push it (origin '$GH_URL' matches no git config dogany.privateRemote glob" <<< "$out" \
   && ! grep -q "^SELECTED=" <<< "$out"; then
  ok "base not on a NON-private origin -> refused (exit 2), never run local silently"
else bad "base_not_on_origin non-private (rc=$rc)"; echo "$out" | sed 's/^/    /'; fi
out="$(select_rt --read-only --repo-contained --repo "$AHEAD")"
grep -q "base auto-push not done -- origin '$GH_URL' matches no git config dogany.privateRemote glob" <<< "$out" \
  && ok "unpushed on a non-private origin: why the auto-push was skipped is printed" || { bad "no auto-push skip note"; echo "$out" | sed 's/^/    /'; }
for why in "base branch not pushed" "post-cut base never reached origin" "needs fonts / sibling repos" "has unpushed commits on the base"; do
  out="$(select_rt --read-only --repo-contained --repo "$PRIV" --local-why "$why")"; rc=$?
  [[ $rc -eq 1 ]] && grep -q "REFUSED: --local-why cites what cloud now handles ('$why')" <<< "$out" && ! grep -q "^SELECTED=" <<< "$out" \
    && ok "--local-why '$why' -> refused (exit 1)" || { bad "local-why '$why' rc=$rc"; echo "$out" | sed 's/^/    /'; }
done
out="$(select_rt --read-only --repo-contained --repo "$CLONE" --local-why "base not pushed and it restarts the launchd service")"
grep -q "^SELECTED=codex " <<< "$out" && ok "--local-why naming a real machine dependency is still accepted" || { bad "machine local-why"; echo "$out" | sed 's/^/    /'; }
printf 'Clone https://github.com/acme/dogany-rehearsal-fonts.git and git@github.com:acme/lifekit-rehearsal for the fonts.\n' > "$TDIR/task-url.md"
out="$(TASK="$TDIR/task-url.md" select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && ok "same-owner GitHub URLs in a brief are not live paths -> cloud" || { bad "same-owner url"; echo "$out" | sed 's/^/    /'; }
printf 'Clone https://github.com/other/kit, then run launchctl kickstart gui/501/kit.\n' > "$TDIR/task-url2.md"
out="$(TASK="$TDIR/task-url2.md" select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
expect_refused_live "another owner's URL is still scanned" launchctl "$out" $rc
printf 'Clone https://github.com/acme/fonts then reload via launchctl restart bot.\n' > "$TDIR/task-url3.md"
out="$(TASK="$TDIR/task-url3.md" select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
expect_refused_live "same-owner URL does not hide live words around it" launchctl "$out" $rc
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
out="$(select_rt --read-only --repo-contained --repo "$AHEAD" --local-why "reloads the live instance launchd job after the build")"
expect_local "ahead of the last fetch + --local-why" unpushed_commits "$out"

echo "== explicit runtime wins (with a stated reason, DGN-1742) =="
out="$(select_rt --read-only --repo-contained --repo "$CLONE" --runtime claude-code --runtime-reason runtime-feature --pin-why "Exercise explicit runtime overrides of cloud first placement (fixture line ${LINENO})")"; rc=$?
if [[ $rc -eq 2 ]] && grep -q '^\[dispatch\] REFUSED: --runtime claude-code (source=flag) on a cloud-eligible run (eligible: target=.* base=main until='"$OPTIN"') needs --local-why "<reason, >=10 chars>" -- or drop the runtime and let cloud-first take it\.$' <<< "$out" \
   && ! grep -q "^SELECTED=" <<< "$out"; then
  ok "--runtime claude-code on an eligible run, no --local-why -> refused (exit 2)"
else bad "explicit claude refusal rc=$rc"; echo "$out" | sed 's/^/    /'; fi
out="$(select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained --runtime codex --runtime-reason runtime-feature --pin-why "Exercise explicit runtime overrides of cloud first placement (fixture line ${LINENO})")"; rc=$?
[[ $rc -eq 2 ]] && grep -q "REFUSED: --runtime codex (source=flag) on a cloud-eligible run" <<< "$out" \
  && ok "--runtime codex on an eligible repo-contained worktree run -> refused" || { bad "explicit codex worktree rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo-contained --repo "$CLONE" --runtime claude-code --runtime-reason runtime-feature --pin-why "Exercise explicit runtime overrides of cloud first placement (fixture line ${LINENO})" --local-why "needs the local MCP credentials")"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
grep -q "^SELECTED=claude-code " <<< "$out" && grep -q "cloud-first: not chosen -- explicit_runtime: claude-code (source=flag)" <<< "$out" \
  && grep -q "skipped by explicit runtime claude-code on a cloud-eligible run -- local-why: needs the local MCP credentials" <<< "$out" \
  && [[ "$(field "$dec" selection_reason)" == explicit_runtime ]] \
  && grep -q "local_why=needs the local MCP credentials" <<< "$(field "$dec" selection_detail)" \
  && ok "--runtime claude-code + --local-why on an eligible run -> claude-code, reason recorded" || { bad "explicit claude + local-why"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --repo-contained --repo "$NONGH" --runtime claude-code --runtime-reason runtime-feature --pin-why "Exercise explicit runtime overrides of cloud first placement (fixture line ${LINENO})")"
grep -q "^SELECTED=claude-code " <<< "$out" && ! grep -q "REFUSED" <<< "$out" \
  && ok "--runtime claude-code on a NON-eligible run needs no --local-why" || { bad "explicit non-eligible"; echo "$out" | sed 's/^/    /'; }
out="$(DOGANY_DISPATCH_RUNTIME=codex select_rt --read-only --repo-contained --repo "$CLONE")"; rc=$?
[[ $rc -eq 2 ]] && grep -q "REFUSED: --runtime codex (source=env) on a cloud-eligible run" <<< "$out" \
  && ok "DOGANY_DISPATCH_RUNTIME=codex on an eligible run, no --local-why -> refused" || { bad "env runtime refusal rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(DOGANY_DISPATCH_RUNTIME=codex select_rt --read-only --repo-contained --repo "$CLONE" --local-why "codex lane is under test here")"
grep -q "^SELECTED=codex " <<< "$out" && grep -q "explicit_runtime: codex (source=env)" <<< "$out" \
  && ok "DOGANY_DISPATCH_RUNTIME=codex + --local-why -> codex" || { bad "env runtime"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --repo "$CLONE" --runtime cloud --pin-why "Exercise explicit runtime overrides of cloud first placement (fixture line ${LINENO})")"
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
            "--read-only --repo $CLONE --runtime claude-code --runtime-reason runtime-feature --pin-why repo-access-declaration-refusal-fixture" "--worktree cf --worktree-repo $CLONE --runtime codex --runtime-reason runtime-feature --pin-why repo-access-declaration-refusal-fixture"; do
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
out="$(select_rt --repo "$CLONE" --runtime cloud --pin-why "Exercise explicit runtime overrides of cloud first placement (fixture line ${LINENO})" --local-why "needs the local MCP credentials")"; rc=$?
# DGN-2164 queues cloud validation refusals and returns 2 (f8bc9ae0).
[[ $rc -eq 2 ]] && grep -q "\-\-local-why given but runtime is 'cloud'" <<< "$out" && ok "--local-why with --runtime cloud -> refused" || { bad "cloud local-why rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only --local-why "needs the local MCP credentials")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "\-\-local-why given on a workspace-only run" <<< "$out" && ok "--local-why on a workspace-only run -> refused" || { bad "ws local-why rc=$rc"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --read-only)"; rc=$?
grep -q "^SELECTED=" <<< "$out" && ! grep -q "REFUSED" <<< "$out" && ok "workspace-only read-only run needs neither flag" || { bad "ws read-only"; echo "$out" | sed 's/^/    /'; }
out="$(select_rt --worktree cf)"; rc=$?
grep -q "^SELECTED=" <<< "$out" && ! grep -q "REFUSED" <<< "$out" && ok "workspace-only worktree run needs neither flag" || { bad "ws worktree"; echo "$out" | sed 's/^/    /'; }

echo "== loss score: the same run, different state =="
fdate() { python3 -c 'import datetime,sys; print((datetime.date.today()+datetime.timedelta(days=int(sys.argv[1]))).isoformat())' "$1"; }
P1="$(fdate 14)" P2="$(fdate 21)" P3="$(fdate 29)"
PASSES="3|$P1:full,$P2:full,$P3:full"
WEEK=$(( 3 * 86400 ))
# The real cloud window is ~4 weeks out (2026-11-05 from 10-09): idle cloud
# capacity then weighs 7/27 -> ~26%.
export DOGANY_CLOUD_FIRST_UNTIL="$(fdate 27)"
out="$(RESET_PASSES_CODEX="$PASSES" CX_RESET_IN=$WEEK select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained)"
dec="$(sed -n 's/^DECISION=//p' <<< "$out")"
# DGN-2293 owner routing policy: a portable (repo-contained) run is placed
# codex-cloud -> cloud-credit -> cloud (within 7d pace) -> claude-code; the
# local codex seat is for host-bound work only, however many passes it has.
if grep -q "^SELECTED=cloud " <<< "$out" \
   && grep -q "^\[dispatch\] runtime selection: cloud (owner_routing_policy; " <<< "$out" \
   && [[ "$(field "$dec" selection_reason)" == owner_routing_policy ]]; then
  ok "repo-contained write run, codex passes + verified hooks -> still off the local codex seat (owner policy: cloud)"
else bad "loss codex passes"; echo "$out" | sed 's/^/    /'; fi

out="$(CX_USED=40 CX_RESET_IN=$(( 12 * 3600 )) select_rt --read-only --repo-contained --repo "$CLONE")"
grep -q "^SELECTED=cloud " <<< "$out" && grep -q "owner policy: cloud" <<< "$out" \
  && ok "no pass, codex week 12h from reset -> portable work still goes to cloud, not the local codex seat" \
  || { bad "loss codex near reset"; echo "$out" | sed 's/^/    /'; }

out="$(CX_USED=57 CX_RESET_IN=$WEEK select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained)"
grep -q "^SELECTED=cloud " <<< "$out" && grep -q "runtime selection: cloud (owner_routing_policy; " <<< "$out" \
  && ok "codex week on pace, no pass -> cloud takes it" \
  || { bad "loss low -> cloud"; echo "$out" | sed 's/^/    /'; }

out="$(RESET_PASSES_CODEX="$PASSES" CX_RESET_IN=$WEEK HOOKS_RC=2 HOOK_NOTE="hooks-unverified@selection: untrusted" \
       select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained)"
if grep -q "^SELECTED=cloud " <<< "$out" \
   && grep -q "vetoed: codex (hooks-unverified@selection, never codex for writes)" <<< "$out"; then
  ok "codex passes but unverified hooks -> never codex for the write run (cloud takes it)"
else bad "unverified hooks"; echo "$out" | sed 's/^/    /'; fi

out="$(RESET_PASSES_CODEX="1|$P3:full" DOGANY_CLOUD_FIRST_UNTIL="$P2" \
       select_rt --worktree cf --worktree-repo "$CLONE" --repo-contained)"
grep -q "^SELECTED=cloud " <<< "$out" && grep -q "codex 0% (1 pass, first ${P3:5})" <<< "$out" \
  && ok "codex pass the burn reaches before it expires -> not at risk, cloud-first" \
  || { bad "pass reached"; echo "$out" | sed 's/^/    /'; }

mkdir -p "$WS/.telegram_bot/dispatch"; echo 8 > "$WS/.telegram_bot/dispatch/active-count.cloud"
out="$(select_rt --read-only --repo-contained --repo "$CLONE")"
rm -f "$WS/.telegram_bot/dispatch/active-count.cloud"
unset DOGANY_CLOUD_FIRST_UNTIL
# DGN-2293: a full cloud lane is no longer vetoed at selection -- the slot walk
# at the gate moves the run to the next lane with room (never queues behind it).
grep -q "^SELECTED=cloud " <<< "$out" && ! grep -qE "vetoed: .*cloud \(at cap" <<< "$out" \
  && ok "cloud at its active cap -> not vetoed at selection (the slot walk re-places it)" \
  || { bad "cloud at cap"; echo "$out" | sed 's/^/    /'; }

echo "== end to end: auto spawn -> status.json =="
out="$("$DD" --read-only --repo-contained --repo "$CLONE" --task "$TDIR/task.md" --bottleneck accuracy \
  --bottleneck-why "verifying a given parser change set" --label cf-e2e 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q "cloud-first: cloud chosen" <<< "$out" && ok "auto spawn exits 0 on cloud" \
  || { bad "e2e spawn rc=$rc"; echo "$out" | sed 's/^/    /'; }
RD="$(grep -l '^LEDGER_BRANCH=claude/cf-e2e$' "$WS/.telegram_bot/dispatch"/dsp-*/run.env 2>/dev/null | head -1 | xargs dirname 2>/dev/null)"
VM="$TDIR/vm"; git clone -q "$ORIGIN" "$VM" 2>/dev/null
n=0; until grep -q '^CLOUD_SESSION_ID=cse' "$RD/cloud.env" 2>/dev/null || [[ $n -ge 40 ]]; do sleep 0.5; n=$((n+1)); done
# The fake session stays running: its final commit must carry the run-specific
# completion marker before the dispatcher can accept the result branch.
RUNID="$(basename "$RD")"
git -C "$VM" -c user.email=t@t -c user.name=t commit -q --allow-empty \
  -m result -m "dispatch-done:$RUNID"
git -C "$VM" push -q origin HEAD:refs/heads/claude/cf-e2e 2>/dev/null
n=0; until [[ -f "$RD/status.json" || $n -ge 60 ]]; do sleep 0.5; n=$((n+1)); done
if [[ -n "$RD" ]] && python3 - "$RD/status.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["runtime"] == "cloud", d.get("runtime")
assert d["selection_reason"] == "owner_routing_policy", d.get("selection_reason")
assert d["result"] == "completed", d.get("result")
PY
then ok "status.json: runtime=cloud, selection_reason=owner_routing_policy, completed"
else bad "status.json ${RD:-no rundir}"; cat "$RD/status.json" 2>/dev/null | head -40; fi

echo
echo "== DGN-1726 cloud-first: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
