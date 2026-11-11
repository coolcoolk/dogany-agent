#!/usr/bin/env bash
# promise: a create whose stored brief differs (e.g. the literal PLACEHOLDER) is disabled and retried once; a second mismatch never runs
# promise: a create reply with no trigger id is recovered by name and still verified, never left untracked
# test-tier: 2
# component: cloud-routine-start
# test-lane: slow    # the timeout case waits out a 1-minute --timeout
# test-host: macos red on Linux, cause not yet triaged (DGN-1920 U8 queue)
# test-dgn1726-cloud-runtime.sh -- cloud sessions as a dispatch runtime.
#
# Owner 2026-09-26: a cloud session must appear exactly like other background
# work -- same ledger row, same batched start/end notices, same workbench row,
# same dispatch-return -- with the model rendered "<Model>".
# r2 (routines transport): failure must reach the owner AT ONCE (owner
# 2026-09-26 17:07: a silent failure leaves the owner not knowing why).
#
# Locks:
#   (a) start: ledger running row (branch claude/<label>, runtime=cloud), the
#       batched START notice line "- Claude Sonnet 5: <name> <HH:MM>", the
#       workbench row "Claude Sonnet 5: <name> [T] · <HH:MM> (<N>분~)", the
#       routine start call (pinned model id, GitHub URL derived from origin,
#       env id from config, base checkout preamble + claude/<label> footer),
#       trigger/session/view recorded, routine disabled after run;
#   (b) gates: local usage gate NOT consulted (stub refuses and logs), own
#       active-count.cloud counter + cap, decision record runtime=cloud;
#   (c) terminal session with branch on origin -> completed with its sha, ledger
#       pending-review, END notice, dispatch-return with
#       branch/sha/view URL, slot released;
#   (d) run ended without the branch -> failed AT ONCE (well before the
#       timeout) with the reason in ledger, status.json and dispatch-return;
#   (e) routine create/run failure -> failed at once, reason + no session;
#   (f) no branch before --timeout -> timed-out path (ledger failed, stop
#       notice, dispatch-return naming the view URL);
#   (g) --cancel: routine disabled, watcher stopped, dispatch-return carries
#       the session link;
#   (h) refusals: existing result branch, no --repo, --worktree, full cap,
#       no CLOUD_ENV_ID, non-GitHub origin, --base not on origin;
#   (l) DGN-1975 --base: default dev reaches the start instruction, a stem
#       --base does too, both recorded in run.env/status.json/selection line
#       and stated in the footer; a base not on origin is refused;
#   (i) lib/cloud_routine.py itself against a fake `claude -p` (body shape,
#       reply parsing, status classification, helper error);
#   (j) r3: after create the stored task text is read back and compared
#       (sha256, byte-exact). Match -> run; mismatch -> the routine is
#       disabled and NEVER run, the dispatch is a start failure with reason
#       "task text mismatch after create (sha a vs b)" (ledger, status.json,
#       notice, dispatch-return).
#
# Safe/offline: the transport helper is a stub (DOGANY_CLOUD_ROUTINE_HELPER)
# and lib/cloud_routine.py runs against a fake `claude`
# (DOGANY_CLOUD_ROUTINE_CLAUDE); bare git repo as origin behind a github.com
# insteadOf rewrite; push/usage-gate stubs; scratch workspace under mktemp.
# NEVER reaches the cloud.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1726-cloud.XXXXXX)"
export DOGANY_RESOURCE_SETTINGS_DIR="$TDIR/resource-settings"
TDIR="$(cd "$TDIR" && pwd -P)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  chflags -R nouappend "$TDIR" 2>/dev/null   # terminal-state ledger is append-only
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/config" "$WS/product" "$WS/.claude" \
         "$WS/.telegram_bot" "$WS/bridge" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
bash "$HARNESS_DIR/fixtures/install-dispatch-policy.sh" "$WS"
cp "$REPO/routines/lib/dispatch_storage.py" "$REPO/routines/lib/conf_reader.py" "$WS/routines/lib/"
for f in ledger-update.sh agentlib.sh dispatch-tier-bindings.tsv conf_reader.py cloud_account.py \
         dispatch-model-display.tsv model_display.py run_block.py i18n_lookup.py cloud_routine.py cloud_completion.sh; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
mkdir -p "$WS/config" && cp -R "$REPO/config/i18n" "$WS/config/"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
cp "$REPO/bridge/machine_gate.py" "$WS/bridge/"
# The local usage gate must never be consulted for a cloud run: this stub
# REFUSES and logs, so any call shows up both as a log line and a refusal.
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo called >> "$USAGE_GATE_LOG"
echo "5h window 97% -- deny"
exit 1
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
TEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --text) TEXT="$2"; shift 2 ;;
    *) shift 1 ;;
  esac
done
printf '%s\n===MSG-END===\n' "$TEXT" >> "$PUSH_LOG"
exit 0
EOF
# Transport stub (stands in for lib/cloud_routine.py). Per-label behavior
# from the task text: FAKE_START_FAIL -> create/run error; otherwise ids
# derived from the label. Run status comes from $FAKE_DIR/status.<trigger>
# (default running); every call is logged.
cat > "$TDIR/bin/cloud-routine-stub" <<'EOF'
#!/bin/bash
action="$1"; shift
printf '%s' "$action" >> "$FAKE_DIR/calls.log"
printf ' %q' "$@" >> "$FAKE_DIR/calls.log"; echo >> "$FAKE_DIR/calls.log"
NAME="" TASKF="" MODEL="" URL="" ENVID="" TRIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2" ;; --task-file) TASKF="$2" ;; --model) MODEL="$2" ;;
    --repo-url) URL="$2" ;; --env-id) ENVID="$2" ;; --trigger-id) TRIG="$2" ;;
  esac
  shift 2
done
case "$action" in
  start)
    lbl="$(printf '%s' "$NAME" | awk '{print $2}')"
    cp "$TASKF" "$FAKE_DIR/task.$lbl"
    printf '%s\n' "$MODEL $URL $ENVID" > "$FAKE_DIR/start.$lbl"
    if grep -q FAKE_START_FAIL "$TASKF"; then
      echo "trigger_id=trg_FAKE_$lbl"; echo "disabled=no"
      echo "error=environment env_TEST not found (404)"; exit 1
    fi
    if grep -q FAKE_TASK_MISMATCH "$TASKF"; then
      # what the real helper does: create -> get differs -> disable, no run
      echo "trigger_id=trg_FAKE_$lbl"; echo "disabled=yes"
      echo "reason=task text mismatch after create (sha 111111111111 vs 222222222222)"
      echo "error=task text mismatch after create (sha 111111111111 vs 222222222222)"; exit 1
    fi
    echo "trigger_id=trg_FAKE_$lbl"; echo "session_id=cse_FAKE_$lbl"
    echo "view=https://claude.ai/code/cse_FAKE_$lbl"; echo "disabled=yes" ;;
  status)
    st="$(cat "$FAKE_DIR/status.$TRIG" 2>/dev/null || echo running)"
    echo "status=$st"
    if [[ "$st" == running ]]; then echo "state=running"
    else echo "state=ended"; echo "reason=final result: tests failed, nothing pushed"; fi ;;
  disable)
    echo "disabled=yes" ;;
esac
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/cloud-routine-stub"

printf 'AGENT_LANG=ko\nCLOUD_ENV_ID=env_TEST01\n' > "$WS/config/agent.conf"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
EOF
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" -c user.email=t@t -c user.name=t add -A >/dev/null
git -C "$WS" -c user.email=t@t -c user.name=t commit -qm seed >/dev/null

# Fake remote: a bare origin reached through a github.com URL (insteadOf in
# the clone's own config), + the local clone the dispatcher is pointed at.
ORIGIN="$TDIR/origin.git"
CLONE="$TDIR/cloudrepo-clone"
GH_URL="https://github.com/acme/widgets.git"
git init -q --bare -b main "$ORIGIN"
git clone -q "$ORIGIN" "$CLONE" 2>/dev/null
git -C "$CLONE" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
git -C "$CLONE" push -q origin HEAD:refs/heads/main 2>/dev/null
git -C "$CLONE" remote set-url origin "$GH_URL"
git -C "$CLONE" config "url.$ORIGIN.insteadOf" "$GH_URL"
# The test lane's push guard (run-tests.sh, DGN-1881) rewrites network push
# URLs to a dead path; the same rule as pushInsteadOf outranks it by length.
git -C "$CLONE" config "url.$ORIGIN.pushInsteadOf" "$GH_URL"
# Stand-in for the cloud session pushing its result (a separate clone, like
# the remote VM's).
VM="$TDIR/vm-clone"
git clone -q "$ORIGIN" "$VM" 2>/dev/null
cloud_push() {  # <label> -> prints the pushed sha
  git -C "$VM" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "cloud result $1"
  git -C "$VM" push -q origin "HEAD:refs/heads/claude/$1" 2>/dev/null
  git -C "$VM" rev-parse HEAD
}

# DGN-2104: owning-login `claude auth status` stub; the cloud is bound to this account.
cat > "$TDIR/bin/claude" <<'CLSTUB'
#!/bin/sh
[ "$1 $2" = "auth status" ] && { echo '{"loggedIn":true,"email":"owner@example.invalid","orgId":"org-owner"}'; exit 0; }
exit 99
CLSTUB
chmod +x "$TDIR/bin/claude"
export DOGANY_CLOUD_ACCOUNT_ID=org-owner
export PATH="$TDIR/bin:$PATH"
export PUSH_LOG="$TDIR/push.log"
export USAGE_GATE_LOG="$TDIR/usage-gate.log"
export FAKE_DIR="$TDIR/fake"
export DOGANY_CLOUD_ROUTINE_HELPER="$TDIR/bin/cloud-routine-stub"
export DOGANY_START_NOTICE_WINDOW=4
export DOGANY_CLOUD_POLL_SECS=1
export DOGANY_CLOUD_STATUS_SECS=2
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_CLOUD_ENV_ID
export DOGANY_DISPATCH_MACHINE_SNAPSHOT="0.5 8 16000"   # DGN-2038 I: idle machine, never the host's live load5
mkdir -p "$FAKE_DIR"
: > "$PUSH_LOG"; : > "$USAGE_GATE_LOG"; : > "$FAKE_DIR/calls.log"
DD="$WS/routines/dispatch-detached.sh"
DISPATCH="$WS/.telegram_bot/dispatch"
LEDGER="$WS/product/auto-loop-ledger.md"
INBOX="$WS/.telegram_bot/session-inbox"
printf 'Refactor the widget parser and add tests.\n' > "$TDIR/task.md"

spawn_cloud() {  # <label> <display-name> [extra args...]
  local label="$1" name="$2"; shift 2
  "$DD" --runtime cloud --pin-why "Exercise cloud transport lifecycle for $label using the fake cloud runner" --repo "$CLONE" --task "${TASK:-$TDIR/task.md}" --model sonnet \
    --bottleneck accuracy --bottleneck-why "verifying a given parser change set" \
    --label "$label" --display-name "$name" --ticket DGN-1726 "$@"
}
rundir_for() {  # <label>
  grep -l "^LEDGER_BRANCH=claude/$1\$" "$DISPATCH"/dsp-*/run.env 2>/dev/null | head -1 | xargs dirname 2>/dev/null
}
wait_for() {  # <secs> <cmd...>
  local n=0 lim=$(( $1 * 2 )); shift
  until "$@"; do
    n=$((n + 1)); [[ $n -ge $lim ]] && return 1
    sleep 0.5
  done
}
row() { grep "^| claude/$1 |" "$LEDGER"; }
row_has() { row "$1" | grep -q "$2"; }
drop_for() { printf '%s/dispatch-%s.md' "$INBOX" "$(basename "$1")"; }

NAME_T="클라우드 제한시간 점검"
NAME_P="위젯 파서 정리"

echo "== (a)(b) start: timeout run + probe run, one burst =="
# DGN-2400: cloud startup now includes the DGN-2373 bounded owner-notice/model
# observation. Keep the timeout fixture live through both sequential spawns;
# one minute can expire before the assertions on a loaded gate host.
out_t="$(spawn_cloud cloud-timeout "$NAME_T" --timeout 2 2>&1)"; rc_t=$?
out_p="$(spawn_cloud cloud-probe "$NAME_P" 2>&1)"; rc_p=$?
[[ $rc_t -eq 0 && $rc_p -eq 0 ]] && ok "both cloud spawns exit 0" \
  || { bad "spawn rc t=$rc_t p=$rc_p"; printf '%s\n---\n%s\n' "$out_t" "$out_p"; }
[[ ! -s "$USAGE_GATE_LOG" ]] && ok "local usage gate never consulted (stub would refuse)" \
  || bad "usage gate was called for a cloud run"
[[ "$(cat "$DISPATCH/active-count.cloud" 2>/dev/null)" == 2 ]] && ok "active-count.cloud = 2" \
  || bad "active-count.cloud = $(cat "$DISPATCH/active-count.cloud" 2>/dev/null)"
# DGN-2400: exercise the cap while both fixtures are known live. Later
# refusal cases now run after enough bounded notice waits for the timeout
# fixture to have legitimately released its slot.
out="$(DOGANY_DISPATCH_MAX_ACTIVE_CLOUD=2 spawn_cloud cap-probe "상한 점검" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "cap reached for runtime=cloud" <<< "$out" && ok "cloud cap enforced (2/2 -> rc 2, no fallback lane)" \
  || bad "cap: rc=$rc $out"
[[ "$(cat "$DISPATCH/active-count.cloud")" == 2 && "$(cat "$DISPATCH/active-count" 2>/dev/null || echo 0)" == 0 ]] \
  && ok "refused spawn moved no counter" || bad "counters after refusal"
[[ "$(cat "$DISPATCH/active-count" 2>/dev/null || echo 0)" == 0 ]] && ok "claude-code slot untouched" \
  || bad "claude-code active-count moved: $(cat "$DISPATCH/active-count")"
grep -q "cloud cap basis:.*cap=" <<< "$out_p" && ok "spawn banner shows the separate cloud cap" \
  || bad "no cloud cap line in banner"
grep -q "model:  Claude Sonnet 5.5 (Cloud)" <<< "$out_p" && ok "spawn banner model = Claude Sonnet 5.5" \
  || bad "banner model line: $(grep 'model:' <<< "$out_p")"
grep -q "routine on https://github.com/acme/widgets (env env_TEST01) from origin/main -> result branch claude/cloud-probe" <<< "$out_p" \
  && ok "banner: GitHub URL from origin, env id from config, base, claude/<label>" || bad "banner cloud line: $(grep 'cloud:' <<< "$out_p")"
row cloud-probe | grep -q "| running |.*model=sonnet runtime=cloud axis=accuracy" \
  && ok "ledger running row claude/cloud-probe runtime=cloud" || bad "ledger row: $(row cloud-probe)"
# DGN-2400: the later notice-ticket-prefix change (9e998dc) deliberately made
# every owner-facing surface lead with the ticket, including the ledger item.
row cloud-probe | grep -q "| \[DGN-1726\] $NAME_P |" && ok "ledger item = ticket-prefixed owner-facing name" \
  || bad "ledger item: $(row cloud-probe)"
dec="$(grep -l '"label": "cloud-probe"' "$DISPATCH"/decisions/*.json 2>/dev/null | head -1)"
if [[ -n "$dec" ]] && python3 - "$dec" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["selected_runtime"] == "cloud", d
assert d["reason_code"] == "cloud_usage_gate_skipped", d
PY
then ok "decision record: selected_runtime=cloud, cloud_usage_gate_skipped"; else bad "decision record ${dec:-missing}"; fi

RD_P="$(rundir_for cloud-probe)"; RD_T="$(rundir_for cloud-timeout)"
wait_for 20 grep -q '^CLOUD_SESSION_ID=cse' "$RD_P/cloud.env" && ok "probe session recorded (cloud.env)" \
  || { bad "no cloud.env"; cat "$RD_P/out.log" "$RD_P/worker.log" 2>/dev/null; }
SID_P="cse_FAKE_cloud-probe"
grep -q "^CLOUD_TRIGGER_ID=trg_FAKE_cloud-probe$" "$RD_P/cloud.env" && grep -q "^CLOUD_TRIGGER_DISABLED=yes$" "$RD_P/cloud.env" \
  && ok "trigger id recorded, routine disabled after run" || bad "cloud.env: $(cat "$RD_P/cloud.env")"
grep -q "^CLOUD_VIEW_URL=https://claude.ai/code/$SID_P$" "$RD_P/cloud.env" && ok "view URL recorded" \
  || bad "view URL: $(cat "$RD_P/cloud.env")"
wait_for 5 grep -q "session=$SID_P" "$LEDGER" && ok "running ledger note carries the session id" \
  || bad "ledger note: $(row cloud-probe)"
[[ "$(cat "$FAKE_DIR/start.cloud-probe" 2>/dev/null)" == "claude-sonnet-5-5 https://github.com/acme/widgets env_TEST01" ]] \
  && ok "start call: pinned model id, derived GitHub URL, configured env id" || bad "start args: $(cat "$FAKE_DIR/start.cloud-probe" 2>/dev/null)"
if python3 - "$FAKE_DIR/task.cloud-probe" <<'PY'
import sys
t = open(sys.argv[1]).read()
first = t.split("\n", 3)
assert "git fetch origin main && git checkout -B work origin/main" in t.split("---")[0], t[:300]
assert "Refactor the widget parser" in t
assert t.index("git fetch origin main") < t.index("Refactor the widget parser")
assert "claude/cloud-probe" in t and "cloud/cloud-probe" not in t.replace("claude/cloud-probe", ""), t
assert "Never push to dev or main" in t and "do not open a pull request" in t, t
assert t.rstrip().endswith("sha: <full commit sha you pushed>"), t[-200:]
PY
then ok "task sent: base checkout first, task, claude/<label> footer (no PR, branch:/sha: lines)"; else bad "task text"; fi

# Workbench row: the real status-footer reader over this scratch ledger.
board="$(DOGANY_LANG=ko DOGANY_LEDGER_FILE="$LEDGER" DOGANY_DISPATCH_DIR="$DISPATCH" DOGANY_LIVE_SHOW_MODEL=off \
  python3 - "$REPO/routines/status-footer.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sf", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for r in m._collect_ledger_rows() or []:
    print("\n".join(r.job_lines()))
PY
)"
echo "$board" | sed 's/^/    board: /'
if python3 - "$board" "$NAME_P" <<'PY'
import re, sys
# Both cloud runs (timeout + probe) are live here, so the board holds two
# blocks; assert the probe's block exactly, line-anchored.
assert re.search(
    r"(?m)^\[DGN-1726\] " + re.escape(sys.argv[2]) + r" [0-9]{2}:[0-9]{2} \([0-9]+분~\)\n"
    r"• 서브에이전트: Claude Sonnet 5\.5 \(Cloud\)$",
    sys.argv[1]), repr(sys.argv[1])
PY
then
  ok "workbench block: name + Claude Sonnet 5.5 (Cloud) actor/time (DGN-2116 eb09387)"
else
  bad "workbench block missing/wrong"
fi

sleep $(( DOGANY_START_NOTICE_WINDOW + 3 ))
start_msg="$(cat "$PUSH_LOG")"
echo "$start_msg" | sed 's/^/    start: /'
grep -q "^▶️ 백그라운드 작업 시작$" <<< "$start_msg" && ok "start notice title" \
  || bad "start notice title"
grep -Eq "^<blockquote>\[DGN-1726\] $NAME_P$" <<< "$start_msg" \
  && grep -Eq "^• 서브에이전트: Claude Sonnet 5\.5 \(Cloud\), [0-9]{2}:[0-9]{2}(</blockquote>)?$" <<< "$start_msg" \
  && ok "start block: ticket-prefixed name + subagent/model/clock" || bad "start line for probe"
grep -q "^status --trigger-id trg_FAKE_cloud-probe --session-id $SID_P" "$FAKE_DIR/calls.log" \
  && ok "watcher polls the run status (trigger + session)" || bad "no status poll: $(cat "$FAKE_DIR/calls.log")"

echo "== (c) result branch plus terminal session -> completed =="
SHA_P="$(cloud_push cloud-probe)"
printf 'completed\n' > "$FAKE_DIR/status.trg_FAKE_cloud-probe"
wait_for 20 test -f "$RD_P/status.json" && ok "probe finalized" || bad "probe never finalized"
if python3 - "$RD_P/status.json" "$SHA_P" "$SID_P" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["result"] == "completed", d["result"]
assert d["runtime"] == "cloud" and d["access_mode"] == "cloud", d
assert d["cloud_branch_sha"] == sys.argv[2], d
assert d["cloud_session"] == sys.argv[3], d
assert d["cloud_trigger"] == "trg_FAKE_cloud-probe" and d["cloud_fail_reason"] == "", d
assert d["model_observed"] == ["claude-sonnet-5-5"], d["model_observed"]
assert d["branch"] == "claude/cloud-probe", d["branch"]
PY
then ok "status.json: completed, sha, session, trigger, model_observed pinned id"; else bad "status.json: $(cat "$RD_P/status.json")"; fi
wait_for 10 row_has cloud-probe "| pending-review |.*cloud 완료 (origin claude/cloud-probe sha=${SHA_P:0:12}" \
  && ok "ledger -> pending-review with sha" || bad "ledger end row: $(row cloud-probe)"
DROP_P="$(drop_for "$RD_P")"
wait_for 5 test -f "$DROP_P" && ok "dispatch-return dropped" || bad "no dispatch-return drop"
sed 's/^/    return: /' "$DROP_P" 2>/dev/null
grep -q "^- model: Claude Sonnet 5.5 (Cloud)$" "$DROP_P" && ok "dispatch-return model: Claude Sonnet 5.5" || bad "drop model line"
grep -q "^- branch: claude/cloud-probe (origin, sha=$SHA_P)$" "$DROP_P" && ok "dispatch-return branch + sha" || bad "drop branch line"
grep -q "^- view: https://claude.ai/code/$SID_P$" "$DROP_P" && ok "dispatch-return view URL" || bad "drop view line"
grep -q "^- routine: trg_FAKE_cloud-probe (disabled=yes)$" "$DROP_P" && ok "dispatch-return routine line" || bad "drop routine line"
grep -q "git fetch origin claude/cloud-probe" "$DROP_P" && ok "dispatch-return recovery directive" || bad "drop directive"
wait_for 70 grep -Eq "^• Claude Sonnet 5\.5 \(Cloud\): \[DGN-1726\] $NAME_P [0-9]{2}:[0-9]{2} \(.*\)$" "$PUSH_LOG" \
  && ok "END notice row: model + ticket-prefixed name/clock/elapsed" || bad "end notice missing"
wait_for 70 grep -q "^✅ .* 작업 완료$" "$PUSH_LOG" && ok "END notice title" || bad "end notice title"
[[ "$(cat "$DISPATCH/active-count.cloud")" == 1 ]] && ok "probe slot released (1 left: timeout run)" \
  || bad "active-count.cloud after completion: $(cat "$DISPATCH/active-count.cloud")"

echo "== (d) run ended without the branch -> failed at once =="
NAME_E="세션 종료 점검"
printf 'ended\n' > "$FAKE_DIR/status.trg_FAKE_cloud-ended"
t0=$(date +%s)
spawn_cloud cloud-ended "$NAME_E" >/dev/null 2>&1 || bad "ended spawn rc"
RD_E="$(rundir_for cloud-ended)"
wait_for 30 test -f "$RD_E/status.json" && ok "ended run finalized in $(( $(date +%s) - t0 ))s (timeout is 120m)" \
  || bad "ended run not finalized"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="error" and d["cloud_branch_sha"]=="" and "session ended (ended) without pushing claude/cloud-ended: final result: tests failed" in d["cloud_fail_reason"], d' \
  "$RD_E/status.json" 2>/dev/null && ok "status.json: error + reason from the run log" || bad "ended status: $(cat "$RD_E/status.json" 2>/dev/null)"
wait_for 10 row_has cloud-ended "| failed |.*cloud 실패 (session=cse_FAKE_cloud-ended): session ended" \
  && ok "ledger -> failed with the reason" || bad "ended ledger: $(row cloud-ended)"
DROP_E="$(drop_for "$RD_E")"
wait_for 5 test -f "$DROP_E" && ok "ended dispatch-return dropped" || bad "no ended drop"
sed 's/^/    return: /' "$DROP_E" 2>/dev/null
grep -q "^- reason: session ended (ended) without pushing claude/cloud-ended: final result: tests failed, nothing pushed$" "$DROP_E" \
  && grep -q "^- view: https://claude.ai/code/cse_FAKE_cloud-ended$" "$DROP_E" \
  && grep -q "ended without pushing.*tell the owner the cause in one line" "$DROP_E" \
  && ok "dispatch-return: reason line, view URL, tell-the-owner directive" || { bad "ended drop"; cat "$DROP_E"; }
wait_for 5 grep -q "^⚠️ 백그라운드 작업 실패" "$PUSH_LOG" && ok "owner END notice (작업실패) sent" || bad "ended notice"
grep "^⚠️ 백그라운드 작업 실패" "$PUSH_LOG" | sed 's/^/    notice: /'

echo "== (e) routine create/run failure -> failed at once =="
NAME_S="루틴 생성 실패"
printf 'FAKE_START_FAIL\n' > "$TDIR/task-fail.md"
TASK="$TDIR/task-fail.md" spawn_cloud cloud-startfail "$NAME_S" >/dev/null 2>&1 || bad "startfail spawn rc"
RD_S="$(rundir_for cloud-startfail)"
wait_for 20 test -f "$RD_S/status.json" && ok "start failure finalized" || bad "start failure not finalized"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="launch-failed" and d["cloud_session"]=="" and d["cloud_fail_reason"]=="routine create/run failed: environment env_TEST not found (404)", d' \
  "$RD_S/status.json" 2>/dev/null && ok "status.json: launch-failed, no session, API reason" || bad "startfail status: $(cat "$RD_S/status.json" 2>/dev/null)"
grep -q "^disable --trigger-id trg_FAKE_cloud-startfail" "$FAKE_DIR/calls.log" \
  && ok "half-created routine disabled" || bad "no disable after failed run"
DROP_S="$(drop_for "$RD_S")"
wait_for 5 test -f "$DROP_S" && grep -q "^- cloud session: (start failed -- no session)$" "$DROP_S" \
  && grep -q "^- reason: routine create/run failed: environment env_TEST not found (404)$" "$DROP_S" \
  && grep -q "the cloud routine failed to start" "$DROP_S" && ok "dispatch-return: no session, reason, directive" \
  || { bad "startfail drop"; cat "$DROP_S" 2>/dev/null; }
wait_for 10 row_has cloud-startfail "| failed |.*cloud 실패 (session=none): routine create/run failed" \
  && ok "ledger -> failed with the reason" || bad "startfail ledger: $(row cloud-startfail)"
wait_for 5 grep -q "^⚠️ 백그라운드 작업 실패" "$PUSH_LOG" && ok "owner END notice (작업실패) sent" || bad "startfail notice"

echo "== (j) task text mismatch after create -> never run, start failure =="
NAME_M="작업 문구 불일치"
printf 'FAKE_TASK_MISMATCH\n' > "$TDIR/task-mismatch.md"
TASK="$TDIR/task-mismatch.md" spawn_cloud cloud-mismatch "$NAME_M" >/dev/null 2>&1 || bad "mismatch spawn rc"
RD_M="$(rundir_for cloud-mismatch)"
wait_for 20 test -f "$RD_M/status.json" && ok "mismatch finalized at once" || bad "mismatch not finalized"
MM_REASON="task text mismatch after create (sha 111111111111 vs 222222222222)"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="launch-failed" and d["cloud_session"]=="" and d["cloud_branch_sha"]=="" and d["cloud_fail_reason"]==sys.argv[2], d' \
  "$RD_M/status.json" "$MM_REASON" 2>/dev/null && ok "status.json: launch-failed, no session, reason = the mismatch (not 'create/run failed')" \
  || bad "mismatch status: $(cat "$RD_M/status.json" 2>/dev/null)"
! grep -q "cloud-mismatch" "$FAKE_DIR/calls.log" | true
[[ "$(grep -c "trg_FAKE_cloud-mismatch" "$FAKE_DIR/calls.log")" == 0 ]] && ! grep -q "^status .*trg_FAKE_cloud-mismatch" "$FAKE_DIR/calls.log" \
  && ok "no run-status poll for the mismatched routine (nothing ran)" || bad "polled a mismatched routine"
DROP_M="$(drop_for "$RD_M")"
wait_for 5 test -f "$DROP_M" && grep -q "^- cloud session: (start failed -- no session)$" "$DROP_M" \
  && grep -q "^- reason: $MM_REASON$" "$DROP_M" && grep -q "^- routine: trg_FAKE_cloud-mismatch (disabled=yes)$" "$DROP_M" \
  && grep -q "the cloud routine failed to start" "$DROP_M" && ok "dispatch-return: start failure, reason = task text mismatch, routine disabled" \
  || { bad "mismatch drop"; cat "$DROP_M" 2>/dev/null; }
sed 's/^/    return: /' "$DROP_M" 2>/dev/null
wait_for 10 row_has cloud-mismatch "| failed |.*cloud 실패 (session=none): $MM_REASON" \
  && ok "ledger -> failed with the mismatch reason" || bad "mismatch ledger: $(row cloud-mismatch)"
wait_for 5 grep -q "^⚠️ 백그라운드 작업 실패" "$PUSH_LOG" && ok "owner END notice (작업실패) sent" || bad "mismatch notice"

echo "== (g) --cancel: routine disabled, session link returned =="
NAME_C="취소 점검"
spawn_cloud cloud-cancel "$NAME_C" >/dev/null 2>&1 || bad "cancel spawn rc"
RD_C="$(rundir_for cloud-cancel)"
wait_for 20 grep -q '^CLOUD_SESSION_ID=cse' "$RD_C/cloud.env" || bad "cancel run never started"
# Pretend the post-run disable had failed so cancel must disable it itself.
wait_for 20 grep -q '^CLOUD_TRIGGER_DISABLED=yes$' "$RD_C/cloud.env" || bad "cancel run disable result never recorded"
sed -i.bak 's/^CLOUD_TRIGGER_DISABLED=yes$/CLOUD_TRIGGER_DISABLED=no/' "$RD_C/cloud.env"
out="$("$DD" --cancel "$(basename "$RD_C")" 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && grep -q "cloud routine trg_FAKE_cloud-cancel disabled=yes; remote session cse_FAKE_cloud-cancel keeps running" <<< "$out" \
  && ok "cancel disables the routine and says the session keeps running" || bad "cancel: rc=$rc $out"
grep -q "^disable --trigger-id trg_FAKE_cloud-cancel" "$FAKE_DIR/calls.log" && ok "disable call made on cancel" || bad "no disable on cancel"
DROP_C="$(drop_for "$RD_C")"
wait_for 10 test -f "$DROP_C" && grep -q "^- view: https://claude.ai/code/cse_FAKE_cloud-cancel$" "$DROP_C" \
  && grep -q "^- reason: canceled here: local watcher stopped, routine disabled" "$DROP_C" \
  && grep -q "^- routine: trg_FAKE_cloud-cancel (disabled=yes)$" "$DROP_C" \
  && ok "cancel dispatch-return: session link, reason, routine disabled" || { bad "cancel drop"; cat "$DROP_C" 2>/dev/null; }

echo "== (h) refusals =="
cloud_push already-there >/dev/null
out="$(spawn_cloud already-there "이미 있는 브랜치" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "claude/already-there already exists on origin" <<< "$out" && ok "existing result branch refused (rc 2)" \
  || bad "existing branch: rc=$rc $out"
# DGN-2144 remains an independent gate: the first existing-branch refusal
# above records this run-specific reason, the next two are still accepted, and
# only the fourth identical pin is refused by the per-day counter.
for _pin_attempt in 2 3; do
  out="$(spawn_cloud already-there "이미 있는 브랜치" 2>&1)"; rc=$?
  [[ $rc -eq 2 ]] && grep -q "claude/already-there already exists on origin" <<< "$out" \
    && ok "repeated pin $_pin_attempt/3 accepted before existing-branch refusal" \
    || bad "repeated pin $_pin_attempt/3: rc=$rc $out"
done
out="$(spawn_cloud already-there "이미 있는 브랜치" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "this --pin-why was already accepted 3 times today" <<< "$out" \
  && ok "fourth identical pin refused by the 3/day gate" || bad "fourth identical pin: rc=$rc $out"
out="$("$DD" --runtime cloud --pin-why "Exercise no-repository cloud validation using the fake cloud runner" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --bottleneck-why "verifying a given parser change set" --label no-repo --display-name "저장소 없음" 2>&1)"; rc=$?
# DGN-2164 queues cloud validation refusals and returns 2 (f8bc9ae0).
[[ $rc -eq 2 ]] && grep -q "requires --repo" <<< "$out" && ok "no --repo refused (rc 2)" || bad "no-repo: rc=$rc $out"
out="$(spawn_cloud wt-probe "워크트리" --worktree x 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "takes no --write/--worktree" <<< "$out" && ok "--worktree refused (rc 1)" || bad "worktree: rc=$rc $out"
printf 'AGENT_LANG=ko\n' > "$WS/config/agent.conf"
out="$(spawn_cloud no-env "환경 없음" 2>&1)"; rc=$?
# DGN-2227: an install without cloud set up gets one plain refusal naming the
# setup path (rc 1), not the old key-level hint.
[[ $rc -eq 1 ]] && grep -q "cloud is not set up on this install" <<< "$out" && grep -q "set up cloud dispatch" <<< "$out" \
  && ok "no CLOUD_ENV_ID refused with the setup path named (rc 1)" \
  || bad "no env: rc=$rc $out"
out="$(DOGANY_CLOUD_ENV_ID=env_OVERRIDE spawn_cloud no-env "환경 없음" --base nope-branch 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "\-\-base nope-branch is not on origin" <<< "$out" && ok "env override accepted; --base not on origin refused (rc 2)" \
  || bad "base: rc=$rc $out"
printf 'AGENT_LANG=ko\nCLOUD_ENV_ID=env_TEST01\n' > "$WS/config/agent.conf"
NG="$TDIR/nongh"
git clone -q "$ORIGIN" "$NG" 2>/dev/null
git -C "$NG" remote set-url origin "https://gitlab.com/acme/widgets.git"
out="$("$DD" --runtime cloud --pin-why "Exercise non-GitHub cloud validation using the fake cloud runner" --repo "$NG" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --bottleneck-why "verifying a given parser change set" --label non-gh --display-name "깃랩" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "not a GitHub repo (https://gitlab.com/acme/widgets.git)" <<< "$out" && ok "non-GitHub origin refused (rc 2)" \
  || bad "non-github: rc=$rc $out"

echo "== (f) no branch before --timeout -> timed-out =="
wait_for 150 test -f "$RD_T/status.json" && ok "timeout run finalized" || bad "timeout run never finalized"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="timed-out" and d["exit_code"]==124 and d["cloud_branch_sha"]=="" and d["cloud_session"], d' \
  "$RD_T/status.json" 2>/dev/null && ok "status.json: timed-out, no sha, session kept" || bad "timeout status: $(cat "$RD_T/status.json")"
wait_for 10 row_has cloud-timeout "| failed |.*cloud timeout (2m, sha=none, claude/cloud-timeout not on origin after 2m" && ok "ledger -> failed (cloud timeout)" \
  || bad "timeout ledger: $(row cloud-timeout)"
DROP_T="$(drop_for "$RD_T")"
wait_for 5 test -f "$DROP_T" && ok "timeout dispatch-return dropped" || bad "no timeout drop"
grep -q "^- model: Claude Sonnet 5.5 (Cloud)$" "$DROP_T" && grep -q "^- branch: claude/cloud-timeout (not on origin)$" "$DROP_T" \
  && grep -q "^- reason: claude/cloud-timeout not on origin after 2m" "$DROP_T" \
  && grep -q "check its state at https://claude.ai/code/cse_FAKE_cloud-timeout" "$DROP_T" \
  && ok "timeout dispatch-return: model, branch absent, reason, view URL directive" || { bad "timeout drop"; cat "$DROP_T"; }
wait_for 5 grep -q "^⚠️ 백그라운드 작업 중단$" "$PUSH_LOG" && ok "timeout -> existing stop notice" || bad "timeout notice"
[[ "$(cat "$DISPATCH/active-count.cloud")" == 0 ]] && ok "all cloud slots released" \
  || bad "active-count.cloud final: $(cat "$DISPATCH/active-count.cloud")"
[[ ! -s "$USAGE_GATE_LOG" ]] && ok "usage gate still never consulted" || bad "usage gate called"

echo "== (l) DGN-1975: --base for --runtime cloud =="
# FAKE_START_FAIL: the stub keeps the task text, then fails the start, so each
# run finalizes at once and holds no slot.
printf 'Base probe task. FAKE_START_FAIL\n' > "$TDIR/task-base.md"
# A failed fixture push read as a product red (base=origin/main) on a loaded
# host; check it and keep git's own words.
for _b in dev dsp/v27-stem; do
  _perr="$(git -C "$CLONE" push -q origin "HEAD:refs/heads/$_b" 2>&1)" \
    || bad "fixture push of $_b to origin failed: $_perr"
done
base_case() {  # <label> <expected base> <expected source> [extra args...]
  local label="$1" base="$2" src="$3"; shift 3
  local out rc rd
  out="$(TASK="$TDIR/task-base.md" spawn_cloud "$label" "기준 브랜치" "$@" 2>&1)"; rc=$?
  [[ $rc -eq 0 ]] || { bad "$label: spawn rc=$rc $out"; return; }
  grep -q "^\[dispatch\] runtime selection: cloud (.*; base=origin/$base ($src); selected_runtime=cloud)$" <<< "$out" \
    && ok "$label: selection line names base=origin/$base ($src)" || bad "$label selection line: $(grep 'runtime selection' <<< "$out")"
  wait_for 20 test -f "$FAKE_DIR/task.$label" || { bad "$label: no start call"; return; }
  if python3 - "$FAKE_DIR/task.$label" "$base" "$src" <<'PY2'
import sys
t, base, src = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]
head = t.split("---")[0]
assert "git fetch origin %s && git checkout -B work origin/%s" % (base, base) in head, head
assert t.index("git checkout -B work origin/%s" % base) < t.index("Base probe task"), t
assert "- Base: origin/%s (%s)." % (base, src) in t, t[-900:]
PY2
  then ok "$label: start instruction checks out origin/$base; footer states the base"; else bad "$label task text"; fi
  rd="$(rundir_for "$label")"
  grep -qxF "WT_BASE=$base" "$rd/run.env" && grep -qx "CLOUD_BASE_SOURCE=$src" "$rd/run.env" \
    && ok "$label: run.env WT_BASE=$base CLOUD_BASE_SOURCE=$src" || bad "$label run.env: $(grep -E '^(WT_BASE|CLOUD_BASE_SOURCE)=' "$rd/run.env")"
  wait_for 20 test -f "$rd/status.json" \
    && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["cloud_base"]==sys.argv[2] and d["cloud_base_source"]==sys.argv[3], d' \
      "$rd/status.json" "$base" "$src" 2>/dev/null \
    && ok "$label: status.json cloud_base=$base ($src)" || bad "$label status.json: $(cat "$rd/status.json" 2>/dev/null)"
}
base_case base-default dev default
base_case base-stem dsp/v27-stem flag --base dsp/v27-stem
out="$(TASK="$TDIR/task-base.md" spawn_cloud base-unknown "기준 브랜치" --base dsp/not-pushed 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "\-\-base dsp/not-pushed is not on origin .*push it first" <<< "$out" \
  && [[ ! -f "$FAKE_DIR/task.base-unknown" ]] && [[ -z "$(rundir_for base-unknown)" ]] \
  && ok "unknown base refused before any routine call (rc 2, push it first)" || bad "unknown base: rc=$rc $out"
wait_for 20 test "$(cat "$DISPATCH/active-count.cloud" 2>/dev/null)" = 0 && ok "base probes released their slots" \
  || bad "active-count.cloud after base probes: $(cat "$DISPATCH/active-count.cloud")"

echo "== (i) lib/cloud_routine.py against a fake claude -p =="
# The fake answers per RemoteTrigger action found in the prompt: create ->
# $FAKE_CREATE, get -> $FAKE_GET_REPLY (default "stored=$FAKE_STORED"), run ->
# $FAKE_RUN; anything else (disable, status) -> $FAKE_REPLY. Each call's action
# is logged to helper-calls, its argv saved (helper-argv.<action>) and its
# prompt saved (helper-prompt.<action>) so the create body can be inspected.
# Like the real CLI, the prompt is read from STDIN only: --allowedTools and
# --disallowedTools are variadic, so a trailing positional prompt would be
# swallowed as a tool name and the real `claude -p` exits 1 with "Input must
# be provided ..." (dsp-20260926-203454-0567). The fake reproduces exactly
# that when stdin is empty.
# Preserve provider replies while exercising the CLI transport hooks. IDs
# come from tool_response; the fake model deliberately echoes no IDs.
cat > "$FAKE_DIR/transport.py" <<'PYTRANSPORT'
import json, os, pathlib, shlex, subprocess, sys

def emit_response(result, argv):
    if '--settings' not in argv:
        print(json.dumps({'is_error': False, 'result': result}))
        return
    hooks = json.loads(argv[argv.index('--settings') + 1])['hooks']
    command = hooks['PreToolUse'][0]['hooks'][0]['command']
    event = {'tool_name': 'RemoteTrigger', 'hook_event_name': 'PreToolUse', 'tool_input': {}}
    pre = subprocess.run(shlex.split(command), input=json.dumps(event), text=True,
                         capture_output=True, check=True)
    request = json.loads(pre.stdout)['hookSpecificOutput']['updatedInput']
    act = request['action']
    directory = pathlib.Path(os.environ['FAKE_DIR'])
    (directory / ('helper-request.' + act)).write_text(json.dumps(request))
    if act == 'create':
        path = directory / 'helper-prompt.create'
        model_prompt = path.read_text()
        (directory / 'helper-model-prompt.create').write_text(model_prompt)
        path.write_text(model_prompt + '\nBODY-BEGIN\n' + json.dumps(request['body']) + '\nBODY-END\n')
    kv = dict(line.split('=', 1) for line in result.splitlines() if '=' in line)
    data, status = {}, 200
    if kv.get('error'):
        status, data = 400, {'error': kv['error']}
    elif act == 'create':
        data = {'trigger': {'id': kv.get('trigger_id', ''), 'enabled': False}}
    elif act == 'list':
        data = {'data': ([{'name': 'dispatch x', 'id': kv['trigger_id'], 'enabled': False}]
                         if kv.get('trigger_id') else []), 'has_more': False}
    elif act == 'get':
        try:
            stored = json.loads(kv.get('stored', 'null'))
        except ValueError:
            stored = None
        data = {'job_config': {'ccr': {'events': [{'data': {'message': {'content': stored}}}]}}}
    elif act == 'run':
        data = {'session_id': kv.get('session_id', '')}
    elif act == 'update':
        data = {'id': request['trigger_id'], 'enabled': kv.get('disabled') != 'yes'}
    event.update(hook_event_name='PostToolUse', tool_input=request,
                 tool_response={'status': status, 'json': json.dumps(data)})
    post = hooks['PostToolUse'][0]['hooks'][0]['command']
    subprocess.run(shlex.split(post), input=json.dumps(event), text=True,
                   capture_output=True, check=True)
    print(json.dumps({'is_error': False, 'result': 'transport captured'}))

if __name__ == '__main__':
    emit_response(sys.argv[1], sys.argv[2:])
PYTRANSPORT
cat > "$TDIR/bin/fake-claude-p" <<'EOF'
#!/bin/bash
prompt="$(cat)"
if [[ -z "$prompt" ]]; then
  echo "Error: Input must be provided either through stdin or as a prompt argument when using --print" >&2
  exit 1
fi
case "$prompt" in
  *"action=create"*) act=create ;; *"action=list "*|*"action=list."*) act=list ;; *"action=get "*) act=get ;;
  *"action=run "*) act=run ;; *"action=update"*) act=disable ;; *) act=other ;;
esac
echo "$act" >> "$FAKE_DIR/helper-calls"
printf '%s\0' "$@" > "$FAKE_DIR/helper-argv.$act"
printf '%s' "$prompt" > "$FAKE_DIR/helper-prompt.$act"
[[ -n "${FAKE_HELPER_ERROR:-}" && "$act" == "${FAKE_HELPER_ERROR_ON:-$act}" ]] && { printf '{"is_error":true,"result":"%s"}' "$FAKE_HELPER_ERROR"; exit 1; }
export ACT="$act"
python3 - "$@" <<'PY'
import json, os, runpy, sys
a = os.environ["ACT"]
r = {"create": os.environ.get("FAKE_CREATE", ""),
     "get": os.environ.get("FAKE_GET_REPLY", "stored=" + os.environ.get("FAKE_STORED", "")),
     "run": os.environ.get("FAKE_RUN", ""),
     "list": os.environ.get("FAKE_LIST", "")}.get(a, os.environ.get("FAKE_REPLY", ""))
runpy.run_path(os.path.join(os.environ['FAKE_DIR'], 'transport.py'))['emit_response'](r, sys.argv[1:])
PY
EOF
chmod +x "$TDIR/bin/fake-claude-p"
CR="$REPO/routines/lib/cloud_routine.py"
export DOGANY_CLOUD_ROUTINE_CLAUDE="$TDIR/bin/fake-claude-p"
calls() { paste -sd, "$FAKE_DIR/helper-calls"; }
cr_start() {  # [task-file] -- one start against the fake; call log reset first
  : > "$FAKE_DIR/helper-calls"
  local result rc=0
  result="$("$CR" start --name "dispatch x" --repo-url https://github.com/acme/widgets --env-id env_TEST01 \
    --model claude-opus-5-5 --task-file "${1:-$TDIR/task.md}" 2>/dev/null)" || rc=$?
  printf '%s\n' "$result" | awk -F= '{a[NR]=$0; last[$1]=NR} END {for(i=1;i<=NR;i++) {split(a[i],k,"="); if(last[k[1]]==i) print a[i]}}'
  return "$rc"
}
json_of() {  # <file> -- the file's exact text as a JSON string literal
  python3 -c 'import json,sys; print(json.dumps(open(sys.argv[1], newline="").read(), ensure_ascii=False))' "$1"
}
export FAKE_REPLY='disabled=yes' FAKE_CREATE='trigger_id=trig_01ABC' FAKE_STORED="$(json_of "$TDIR/task.md")" \
  FAKE_RUN=$'session_id=cse_01XYZ\nview=none\ndisabled=yes'
out="$(cr_start)"; rc=$?
[[ $rc -eq 0 && "$out" == $'trigger_id=trig_01ABC\nsession_id=cse_01XYZ\nview=https://claude.ai/code/cse_01XYZ\ndisabled=yes' ]] \
  && ok "start: stored text matches -> ids parsed, falls back to the claude.ai/code view URL" || bad "start parse: rc=$rc $out"
[[ "$(calls)" == "create,get,run,disable" ]] && ok "start order: create -> get (verify) -> run" || bad "call order: $(calls)"
if python3 - "$FAKE_DIR/helper-argv.create" "$FAKE_DIR/helper-prompt.get" "$FAKE_DIR/helper-prompt.run" "$FAKE_DIR/helper-prompt.create" <<'PY'
import datetime, json, sys
def argv(p):
    return [x.decode() for x in open(p, "rb").read().split(b"\0")[:-1]]
def prompt(p):
    return open(p, encoding="utf-8").read()
a = argv(sys.argv[1])
assert a[:3] == ["-p", "--model", "sonnet"], a[:3]
assert a[a.index("--allowedTools") + 1] == "ToolSearch RemoteTrigger", a
assert "Bash" in a[a.index("--disallowedTools") + 1].split(), a
# the variadic tool list is the LAST argv item: no positional prompt after it
assert a.index("--disallowedTools") == len(a) - 2, a
p = prompt(sys.argv[4])
body = json.loads(p.split("BODY-BEGIN\n", 1)[1].split("\nBODY-END", 1)[0])
model_prompt = open(sys.argv[4].replace('helper-prompt', 'helper-model-prompt')).read()
assert 'Refactor the widget' not in model_prompt
ccr = body["job_config"]["ccr"]
assert body["enabled"] is False
t = datetime.datetime.strptime(body["run_once_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
assert t > datetime.datetime.now(datetime.timezone.utc), body["run_once_at"]
assert ccr["environment_id"] == "env_TEST01"
sc = ccr["session_context"]
assert sc["model"] == "claude-opus-5-5"
assert sc["sources"] == [{"git_repository": {"url": "https://github.com/acme/widgets"}}]
assert sc["allowed_tools"] == ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
ev = ccr["events"][0]["data"]
assert ev["type"] == "user" and ev["session_id"] == "" and ev["parent_tool_use_id"] is None
assert len(ev["uuid"]) == 36
assert ev["message"] == {"role": "user", "content": "Refactor the widget parser and add tests.\n"}
assert "action=create" in p and "action=run" not in p
g = prompt(sys.argv[2])
assert "action=get with trigger_id=trig_01ABC" in g and "Refactor the widget" not in g, g  # fresh turn: no task text
r = prompt(sys.argv[3])
assert "action=run with trigger_id=trig_01ABC" in r, r
PY
then ok "start: helper argv (sonnet, RemoteTrigger only, prompt on stdin) + create body shape; read-back prompt carries no task text"; else bad "helper argv/body"; fi
# Regression (dsp-20260926-203454-0567): a ~1900B task used to die at create
# with "local claude -p helper rc=1 ... Input must be provided ...".
python3 -c 'import sys; sys.stdout.write(("Review the dispatcher cloud lane. " * 56)[:1900] + "\n")' > "$TDIR/task-1900.md"
out="$(FAKE_CREATE='trigger_id=trig_01ABC' FAKE_STORED="$(json_of "$TDIR/task-1900.md")" \
  FAKE_RUN=$'session_id=cse_01XYZ\nview=none\ndisabled=yes' cr_start "$TDIR/task-1900.md")"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,get,run,disable" ]] && ! grep -q "Input must be provided" <<< "$out" \
  && ok "regression: 1900B task reaches create/get/run with the prompt on stdin (no 'Input must be provided')" \
  || bad "1900B task: rc=$rc $out"

echo "== (j2) task text mismatch: helper level =="
MM_STORED='"Refactor the widget parser and add tests."'   # trailing newline lost
WANT_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:12])' "$TDIR/task.md")"
GOT_SHA="$(python3 -c 'import hashlib; print(hashlib.sha256(b"Refactor the widget parser and add tests.").hexdigest()[:12])')"
out="$(FAKE_STORED="$MM_STORED" FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 ]] && grep -qx "trigger_id=trig_01ABC" <<< "$out" && grep -qx "disabled=yes" <<< "$out" \
  && grep -qx "reason=task text mismatch after create (sha $WANT_SHA vs $GOT_SHA)" <<< "$out" \
  && ok "mismatch (one trailing newline lost): rc 1, routine disabled, reason names both shas" || bad "mismatch: rc=$rc $out"
echo "$out" | sed 's/^/    mismatch: /'
[[ "$(calls)" == "create,get,disable,create,get,disable" ]] && ok "mismatch twice: the run call is NEVER made (create,get,disable x2)" || bad "mismatch calls: $(calls)"
out="$(FAKE_STORED="$MM_STORED" FAKE_REPLY='disabled=no' cr_start)"
grep -qx "disabled=no" <<< "$out" && grep -q "^reason=task text mismatch" <<< "$out" \
  && ok "mismatch + disable unconfirmed -> disabled=no reported (dispatcher retries the disable)" || bad "mismatch/no-disable: $out"
out="$(FAKE_GET_REPLY='error=trigger not found' FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,get,disable" ]] \
  && grep -q "^reason=task text unverified after create (get failed: RemoteTrigger HTTP 400: {'error': 'trigger not found'})" <<< "$out" \
  && ok "read-back error -> unverified, disabled, never run" || bad "get error: rc=$rc $out"
out="$(FAKE_GET_REPLY='stored=not-json' FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,get,disable" ]] \
  && grep -q "^reason=task text unverified after create (no stored content in helper reply)" <<< "$out" \
  && ok "garbled read-back -> unverified, disabled, never run" || bad "garbled: rc=$rc $out"
printf 'caf\xc3\xa9 "quoted" \\ back\r\nline2\n\n' > "$TDIR/task-odd.md"
out="$(FAKE_STORED="$(json_of "$TDIR/task-odd.md")" cr_start "$TDIR/task-odd.md")"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,get,run,disable" ]] \
  && ok "byte-exact: non-ASCII, quotes, backslash, CRLF, trailing blank line all round-trip" || bad "odd text: rc=$rc $out"
out="$(FAKE_STORED="$(json_of "$TDIR/task-odd.md")" cr_start "$TDIR/task.md")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "^reason=task text mismatch after create" <<< "$out" \
  && ok "a different stored text is a mismatch" || bad "different text: rc=$rc $out"
out="$(FAKE_CREATE='error=repository not authorized' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,list" ]] && grep -q 'repository not authorized' <<< "$out" \
  && ok "create API error -> rc 1, confirmed not-started, error line, nothing else called" || bad "create error: rc=$rc $out"
out="$(FAKE_RUN=$'error=quota exceeded\ndisabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 ]] && grep -qx 'trigger_id=trig_01ABC' <<< "$out" && grep -qx 'disabled=yes' <<< "$out" && grep -q 'quota exceeded' <<< "$out" \
  && ok "run API error after a verified create -> rc 1, disable does not prove it never started" || bad "run error: rc=$rc $out"
out="$(FAKE_HELPER_ERROR=boom FAKE_HELPER_ERROR_ON=create cr_start)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "^error=.*boom" <<< "$out" && ok "create helper failure -> rc 1 with error" \
  || bad "helper failure: rc=$rc $out"
out="$(FAKE_HELPER_ERROR=boom FAKE_HELPER_ERROR_ON=run FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,get,run,disable,other" ]] \
  && grep -qx "trigger_id=trig_01ABC" <<< "$out" && grep -q "^error=.*boom" <<< "$out" \
  && grep -qx "not_started=unknown" <<< "$out" \
  && ok "run-call helper failure -> routine disabled but attempted execution stays unknown" || bad "run helper failure: rc=$rc $out"
unset FAKE_CREATE FAKE_STORED FAKE_RUN
out="$(FAKE_REPLY=$'status=running\nstate=running' "$CR" status --trigger-id trg_1 --session-id cse_1)"
[[ "$out" == $'status=running\nstate=running' ]] && ok "status: running" || bad "status running: $out"
out="$(FAKE_REPLY=$'status=idle\nstate=running\nreason=pushed nothing' "$CR" status --trigger-id trg_1)"
[[ "$out" == $'status=idle\nstate=ended\nreason=pushed nothing' ]] && ok "status: raw idle overrides the model's guess -> ended + reason" \
  || bad "status idle: $out"
out="$(FAKE_REPLY=$'status=weird\nstate=bogus' "$CR" status --trigger-id trg_1)"
[[ "$out" == $'status=weird\nstate=unknown' ]] && ok "status: unknown raw status -> unknown (keep waiting)" || bad "status unknown: $out"
out="$(FAKE_REPLY='disabled=yes' "$CR" disable --trigger-id trg_1)"; rc=$?
[[ $rc -eq 0 && "$out" == "disabled=yes" ]] && ok "disable: confirmed" || bad "disable: rc=$rc $out"
out="$("$CR" disable --trigger-id 'trg;rm')"; rc=$?
[[ $rc -eq 1 ]] && grep -q "^error=invalid id" <<< "$out" && ok "ids are validated before any call" || bad "id validation: $out"

echo "== (k) DGN-1924: trigger id copied verbatim, re-resolved by name =="
export FAKE_STORED="$(json_of "$TDIR/task.md")" FAKE_RUN=$'session_id=cse_01XYZ\nview=none\ndisabled=yes'
out="$(FAKE_CREATE='trigger_id=trg_X1' FAKE_LIST='trigger_id=trig_X1' cr_start)"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,list,get,run,disable" ]] && grep -qx "trigger_id=trig_X1" <<< "$out" \
  && grep -q "action=get with trigger_id=trig_X1" "$FAKE_DIR/helper-prompt.get" \
  && grep -q 'action=list' "$FAKE_DIR/helper-prompt.list" \
  && ok "helper reply trg_X1 + list trig_X1 -> emitted trigger_id=trig_X1, get/run use it" || bad "re-resolve: rc=$rc calls=$(calls) $out"
out="$(FAKE_CREATE='trigger_id=trig_X1' cr_start)"
[[ "$(calls)" == "create,get,run,disable" ]] && ok "valid trig_ id -> no list call" || bad "valid id list call: $(calls)"
out="$(FAKE_CREATE='trigger_id=trg_X1' FAKE_LIST='error=no routine named that' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,list" ]] && grep -q "^error=no trigger id in helper reply and none found by name" <<< "$out" \
  && ok "unresolvable bad id -> failure, nothing run" || bad "unresolvable: rc=$rc $(calls) $out"
out="$(FAKE_CREATE='trigger_id=trig_X1' FAKE_GET_REPLY='error=HTTP 400 invalid trigger ID format' FAKE_LIST='trigger_id=trig_X2' cr_start)"; rc=$?
[[ "$(calls)" == "create,get,list,get,disable" ]] && grep -qx "trigger_id=trig_X2" <<< "$out" \
  && ok "get invalid-id error -> list by name, ONE retry with the resolved id, then failure (id emitted)" || bad "get retry: rc=$rc $(calls) $out"
out="$(FAKE_REPLY='error=invalid trigger ID format' FAKE_LIST='trigger_id=trig_X3' "$CR" disable --trigger-id trg_X3 --name "dispatch x")"; rc=$?
[[ $rc -eq 1 ]] && ok "disable retry that still fails -> rc 1" || bad "disable fail: rc=$rc $out"
out="$(FAKE_REPLY='disabled=yes' "$CR" disable --trigger-id trg_X3 --name "dispatch x")"
[[ "$out" == "disabled=yes" ]] && ok "disable ok first time -> no resolve" || bad "disable ok: $out"
: > "$FAKE_DIR/helper-calls"
cat > "$TDIR/bin/fake-claude-2" <<'EOS'
#!/bin/bash
prompt="$(cat)"
if [[ "$prompt" == *"action=list "* ]]; then r='trigger_id=trig_X3'
elif [[ "$prompt" == *"trigger_id=trig_X3"* ]]; then r='disabled=yes'
else r='error=HTTP 400 invalid trigger ID format'; fi
echo "$prompt" | grep -o 'action=[a-z]* ' | head -1 >> "$FAKE_DIR/helper-calls"
python3 "$FAKE_DIR/transport.py" "$r" "$@"
EOS
chmod +x "$TDIR/bin/fake-claude-2"
out="$(DOGANY_CLOUD_ROUTINE_CLAUDE="$TDIR/bin/fake-claude-2" "$CR" disable --trigger-id trg_X3 --name "dispatch x")"; rc=$?
[[ $rc -eq 0 ]] && grep -qx 'trigger_id=trig_X3' <<< "$out" && grep -qx 'disabled=yes' <<< "$out" && ok "disable: trg_X3 invalid -> list -> disabled with trig_X3" || bad "disable resolve: rc=$rc $out"
unset FAKE_STORED FAKE_RUN FAKE_CREATE FAKE_LIST FAKE_GET_REPLY

echo "== (m) DGN-2220: placeholder body retried once; truncated create reply recovered by name =="
python3 -c 'import sys; sys.stdout.write("Large brief line. " * 4000 + "\n")' > "$TDIR/task-big.md"
export FAKE_CREATE='trigger_id=trig_01ABC' FAKE_RUN=$'session_id=cse_01XYZ\nview=none\ndisabled=yes' FAKE_REPLY='disabled=yes'
cat > "$TDIR/bin/fake-claude-ph" <<'EOS'
#!/bin/bash
# first get returns the PLACEHOLDER body, later gets return the real text
prompt="$(cat)"
case "$prompt" in
  *"action=create"*) act=create; r='trigger_id=trig_01ABC' ;;
  *"action=get "*) act=get
     n=$(cat "$FAKE_DIR/get-n" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_DIR/get-n"
     if [[ $n -eq 0 ]]; then r='stored="PLACEHOLDER"'; else r="stored=$FAKE_STORED"; fi ;;
  *"action=run "*) act=run; r=$'session_id=cse_01XYZ\nview=none\ndisabled=yes' ;;
  *) act=disable; r='disabled=yes' ;;
esac
echo $act >> "$FAKE_DIR/helper-calls"
python3 "$FAKE_DIR/transport.py" "$r" "$@"
EOS
chmod +x "$TDIR/bin/fake-claude-ph"
rm -f "$FAKE_DIR/get-n"
out="$(FAKE_STORED="$(json_of "$TDIR/task-big.md")" DOGANY_CLOUD_ROUTINE_CLAUDE="$TDIR/bin/fake-claude-ph" cr_start "$TDIR/task-big.md")"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,get,disable,create,get,run,disable" ]] && grep -qx "session_id=cse_01XYZ" <<< "$out" \
  && ok "76KB brief, first stored body PLACEHOLDER -> bad routine disabled, create retried, second verified, run" || bad "placeholder retry: rc=$rc $(calls) $out"
out="$(FAKE_CREATE='noise without id' FAKE_LIST='trigger_id=trig_01ABC' FAKE_STORED="$(json_of "$TDIR/task.md")" cr_start)"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,list,get,run,disable" ]] && grep -qx "trigger_id=trig_01ABC" <<< "$out" \
  && ok "create reply without trigger id -> found by name via list, verified, run (no orphan)" || bad "orphan recovery: rc=$rc $(calls) $out"
out="$(FAKE_CREATE='noise without id' FAKE_LIST='error=none' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,list" ]] && grep -q "^error=no trigger id in helper reply and none found by name" <<< "$out" \
  && ok "no id and not listed -> failure, nothing run" || bad "orphan unresolved: rc=$rc $(calls) $out"
unset FAKE_CREATE FAKE_RUN FAKE_REPLY FAKE_STORED

echo
echo "== DGN-1726 cloud runtime: $PASS passed, $FAIL failed =="
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
