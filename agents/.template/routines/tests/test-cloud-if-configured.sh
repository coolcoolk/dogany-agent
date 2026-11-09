#!/usr/bin/env bash
# test-lane: slow    # spawns the dispatcher probe ~15 times (cf. test-dgn1726-cloud-first.sh)
# test-component: cloud-configuration
# test-tier: 2
# test-prereq: fixture-repo, tmp-files, subprocess
# test-new-component: Unconfigured-install option and notice suppression needs a separate configuration matrix from cloud-first routing.
# test-dgn2227-cloud-if-configured.sh -- cloud runtimes only on installs that set cloud up.
#
# Owner 2026-10-22 14:06 (dec-435): cloud is for installs that set it up; an
# install without it never sees it as an option, and a user who wants it is
# helped to set it up. Locks:
#   UNCONFIGURED (no CLOUD_ENV_ID, no Codex cloud environment; CLOUD_FIRST_UNTIL
#   set to prove the window alone enables nothing):
#     auto read-only / repo-contained worktree / plain --repo runs -> a local
#       runtime; stdout+stderr, the decision record and the start/end notice
#       text carry no "cloud" word (no cloud-first line, no vetoed cloud pool)
#     --repo without --repo-contained/--local-why -> accepted (the cloud
#       forcing points do not apply)
#     explicit --runtime cloud / codex-cloud / --cloud-extra-repo -> exactly one
#       plain refusal: the setup line, no env ids or config keys
#     the shipped template agent.conf configures no cloud
#   CONFIGURED: eligible run -> cloud as today; a Codex cloud environment ->
#     codex-cloud is weighed again; the DGN-2104 account gate, where present
#     (cloud_account_check), failing -> cloud absent.
# Offline: bare origin behind a github.com insteadOf rewrite, stubbed gates.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

# The scratch path must not itself carry the word under test.
TDIR="$(mktemp -d /tmp/dgn2227.XXXXXX)"
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
for f in ledger-update.sh agentlib.sh dispatch-tier-bindings.tsv conf_reader.py \
         dispatch-model-display.tsv model_display.py cloud_routine.py cloud_completion.sh \
         reset_passes.py runtime_loss.py runtime_headroom.py api_credit_pool.py codex_cloud.py cloud_account.py; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
cp "$REPO/bridge/machine_gate.py" "$WS/bridge/"
printf '#!/bin/bash\necho "5h window 97%% -- deny"\nexit 1\n' > "$WS/routines/usage-gate.sh"
printf '#!/bin/bash\nexit 0\n' > "$WS/routines/push.sh"
printf '#!/bin/bash\nexit 0\n' > "$TDIR/bin/codex"
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/codex"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
EOF
UNCONF=$'AGENT_LANG=en\nCLOUD_FIRST_UNTIL=2099-12-31\n'
CONF=$'AGENT_LANG=en\nCLOUD_ENV_ID=env_TEST01\nCLOUD_ACCOUNT_ID=org-owner\nCLOUD_FIRST_UNTIL=2099-12-31\n'
printf '%s' "$UNCONF" > "$WS/config/agent.conf"
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" -c user.email=t@t -c user.name=t add -A >/dev/null
git -C "$WS" -c user.email=t@t -c user.name=t commit -qm seed >/dev/null

ORIGIN="$TDIR/origin.git"
GH_URL="https://github.com/acme/widgets.git"
git init -q --bare -b main "$ORIGIN"
mkclone() {
  git clone -q "$ORIGIN" "$1" 2>/dev/null
  git -C "$1" remote set-url origin "$GH_URL"
  git -C "$1" config "url.$ORIGIN.insteadOf" "$GH_URL"
  git -C "$1" config "url.$ORIGIN.pushInsteadOf" "$GH_URL"
}
SEED="$TDIR/seed"
git clone -q "$ORIGIN" "$SEED" 2>/dev/null
git -C "$SEED" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
git -C "$SEED" push -q origin HEAD:refs/heads/main 2>/dev/null
CLONE="$TDIR/repo"; mkclone "$CLONE"

# The configured account is fixture data, never the host's login/keychain.
cat > "$TDIR/bin/claude" <<'CLSTUB'
#!/bin/sh
[ "$1 $2" = "auth status" ] && { echo '{"loggedIn":true,"email":"owner@example.invalid","orgId":"org-owner"}'; exit 0; }
exit 99
CLSTUB
chmod +x "$TDIR/bin/claude"
export PATH="$TDIR/bin:$PATH"
export DOGANY_START_NOTICE_WINDOW=1
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_CLOUD_ENV_ID DOGANY_CLOUD_FIRST_UNTIL \
      DOGANY_DISPATCH_RUNTIME DOGANY_CODEX_CLOUD_ENV_ID DOGANY_CLOUD_ACCOUNT_ID
export DOGANY_DISPATCH_MACHINE_SNAPSHOT="0.5 8 16000" DOGANY_DISPATCH_MACHINE_PRESSURE=off
DD="$WS/routines/dispatch-detached.sh"
printf 'Review the widget parser for off-by-one errors.\n' > "$TDIR/task.md"

# probe <args...> -- do_spawn up to the tier gate, then the owner notice
# text the selected run would send (start line + end block).
probe() {
  local dec="$TDIR/decisions.$RANDOM$RANDOM"
  DISPATCH_TEST=1 DEC="$dec" bash -c '
source "$0"
DECISION_DIR="$DEC"
print_calibration() { :; }
observe_sandbox_available() { :; }
codex_quota_gate() { CODEX_USED_PERCENT=10; CODEX_QUOTA_NOTE=fixture; CODEX_RESETS_AT=""; return 0; }
codex_hook_gate() { CODEX_HOOK_NOTE=fixture; return 0; }
[[ -n "${ACCOUNT_GATE:-}" ]] && cloud_account_check() { return "$ACCOUNT_GATE"; }
bottleneck_gate() {
  echo "SELECTED=$RUNTIME"
  echo "NOTICE_START=$(owner_model_display_cap)"
  echo "NOTICE_END=$(end_notice_text completed "widget review" 12:00 90 | tr "\n" " ")"
  echo "DECISION=$(ls "$DECISION_DIR"/*.json 2>/dev/null | head -1)"
  exit 0
}
do_spawn' "$DD" --task "$TDIR/task.md" --bottleneck accuracy \
    --bottleneck-why "verifying a given parser change set" --label "${LBL:-p2227}" "$@" 2>&1
}
decision() { local d; d="$(sed -n 's/^DECISION=//p' <<< "$1")"; [[ -n "$d" ]] && cat "$d"; }
selected() { sed -n 's/^SELECTED=//p' <<< "$1"; }

# expect_no_cloud <name> <output>: local runtime, no cloud word anywhere the
# owner or the decision record would show it.
expect_no_cloud() {
  local out="$2" rt dj
  rt="$(selected "$out")"; dj="$(decision "$out")"
  if [[ -n "$rt" && "$rt" != cloud && "$rt" != codex-cloud && -n "$dj" ]] \
     && ! grep -qi cloud <<< "$out" && ! grep -qi cloud <<< "$dj"; then
    ok "$1 -> $rt, no cloud word in output, decision record or notices"
  else
    bad "$1: expected a local runtime and zero cloud mentions (selected '${rt:-none}')"
    { echo "$out"; echo "$dj"; } | grep -i -m8 -e cloud -e SELECTED | sed 's/^/    /'
  fi
}
# expect_setup_refusal <name> <line> <output> <rc>
expect_setup_refusal() {
  local n
  n="$(grep -c 'REFUSED' <<< "$3")"
  if [[ $4 -ne 0 && "$n" == 1 ]] && grep -qF "[dispatch] REFUSED: $2" <<< "$3" \
     && ! grep -q '^SELECTED=' <<< "$3" && ! grep -qE 'CLOUD_ENV_ID|env_|agent\.conf|codexCloudEnvironment' <<< "$3"; then
    ok "$1 -> one plain refusal naming the setup path"
  else
    bad "$1: expected exactly the setup refusal (rc=$4)"; echo "$3" | sed 's/^/    /'
  fi
}
SETUP="cloud is not set up on this install, so this run cannot go to the cloud. To use it, ask your agent to set up cloud dispatch; until then runs stay on this machine."
CX_SETUP="Codex cloud is not set up for this repository, so this run cannot go there. To use it, ask your agent to set up Codex cloud; until then runs stay on this machine."

echo "== unconfigured install (CLOUD_FIRST_UNTIL set, no environment) =="
expect_no_cloud "read-only --repo-contained --repo" "$(probe --read-only --repo-contained --repo "$CLONE")"
expect_no_cloud "repo-contained worktree run" "$(probe --worktree cf --worktree-repo "$CLONE" --repo-contained)"
expect_no_cloud "plain --repo (no --repo-contained / --local-why): not refused" \
  "$(probe --read-only --repo "$CLONE")"
expect_no_cloud "workspace-only read-only run" "$(probe --read-only)"
expect_no_cloud "window via env alone" "$(DOGANY_CLOUD_FIRST_UNTIL=2099-12-31 probe --read-only --repo-contained --repo "$CLONE")"
out="$(probe --runtime cloud --read-only --repo "$CLONE")"; rc=$?
expect_setup_refusal "explicit --runtime cloud" "$SETUP" "$out" $rc
out="$(DOGANY_DISPATCH_RUNTIME=cloud probe --read-only --repo-contained --repo "$CLONE")"; rc=$?
expect_setup_refusal "runtime cloud from the environment" "$SETUP" "$out" $rc
out="$(probe --runtime codex-cloud --repo-contained --repo "$CLONE")"; rc=$?
expect_setup_refusal "explicit --runtime codex-cloud" "$CX_SETUP" "$out" $rc
out="$(probe --read-only --repo-contained --repo "$CLONE" --cloud-extra-repo https://github.com/acme/other)"; rc=$?
expect_setup_refusal "--cloud-extra-repo" "$SETUP" "$out" $rc

echo "== shipped template =="
TPL="$REPO/config/agent.conf"
tv="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("CLOUD_ENV_ID", sys.argv[2]) + "|" + conf_get("CLOUD_FIRST_UNTIL", sys.argv[2]))' "$REPO/routines/lib" "$TPL")"
[[ "$tv" == "|" ]] && ok "template agent.conf: CLOUD_ENV_ID and CLOUD_FIRST_UNTIL unset" \
  || bad "template agent.conf configures cloud: '$tv'"
cp "$TPL" "$WS/config/agent.conf"
expect_no_cloud "fresh install on the template conf" "$(probe --read-only --repo-contained --repo "$CLONE")"

echo "== configured install: unchanged =="
printf '%s' "$CONF" > "$WS/config/agent.conf"
out="$(probe --read-only --repo-contained --repo "$CLONE")"
[[ "$(selected "$out")" == cloud ]] && grep -q 'cloud-first: cloud chosen' <<< "$out" \
  && ok "eligible run -> cloud, as before" || { bad "configured eligible run"; echo "$out" | sed 's/^/    /'; }
out="$(ACCOUNT_GATE=1 probe --read-only --repo-contained --repo "$CLONE")"
if [[ "$(selected "$out")" != cloud ]] && ! grep -q 'cloud-first' <<< "$out"; then
  ok "account gate present and failing -> cloud absent"
else bad "account gate failing still offered cloud"; echo "$out" | sed 's/^/    /'; fi
out="$(ACCOUNT_GATE=0 probe --read-only --repo-contained --repo "$CLONE")"
[[ "$(selected "$out")" == cloud ]] && ok "account gate present and passing -> cloud" \
  || { bad "account gate passing"; echo "$out" | sed 's/^/    /'; }
printf '%s' "$UNCONF" > "$WS/config/agent.conf"
CX="$TDIR/repo-cx"; mkclone "$CX"; git -C "$CX" config dogany.codexCloudEnvironment env-cx01
out="$(LBL=p2227cx probe --worktree cx --worktree-repo "$CX" --repo-contained)"
grep -q 'codex-cloud' <<< "$out$(decision "$out")" && ok "Codex cloud environment set -> codex-cloud weighed again" \
  || { bad "configured codex-cloud not weighed"; echo "$out" | sed 's/^/    /'; }

echo
echo "== DGN-2227 cloud-if-configured: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
