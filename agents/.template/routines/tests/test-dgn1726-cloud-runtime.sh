#!/usr/bin/env bash
# test-lane: slow    # the timeout case waits out a 1-minute --timeout
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
#   (c) branch appears on origin -> completed with its sha, ledger
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
for f in ledger-update.sh agentlib.sh dispatch-tier-bindings.tsv conf_reader.py \
         dispatch-model-display.tsv model_display.py cloud_routine.py; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
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
# Stand-in for the cloud session pushing its result (a separate clone, like
# the remote VM's).
VM="$TDIR/vm-clone"
git clone -q "$ORIGIN" "$VM" 2>/dev/null
cloud_push() {  # <label> -> prints the pushed sha
  git -C "$VM" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "cloud result $1"
  git -C "$VM" push -q origin "HEAD:refs/heads/claude/$1" 2>/dev/null
  git -C "$VM" rev-parse HEAD
}

export PATH="$TDIR/bin:$PATH"
export PUSH_LOG="$TDIR/push.log"
export USAGE_GATE_LOG="$TDIR/usage-gate.log"
export FAKE_DIR="$TDIR/fake"
export DOGANY_CLOUD_ROUTINE_HELPER="$TDIR/bin/cloud-routine-stub"
export DOGANY_START_NOTICE_WINDOW=4
export DOGANY_CLOUD_POLL_SECS=1
export DOGANY_CLOUD_STATUS_SECS=2
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_CLOUD_ENV_ID
mkdir -p "$FAKE_DIR"
: > "$PUSH_LOG"; : > "$USAGE_GATE_LOG"; : > "$FAKE_DIR/calls.log"
DD="$WS/routines/dispatch-detached.sh"
DISPATCH="$WS/.telegram_bot/dispatch"
LEDGER="$WS/product/auto-loop-ledger.md"
INBOX="$WS/.telegram_bot/session-inbox"
printf 'Refactor the widget parser and add tests.\n' > "$TDIR/task.md"

spawn_cloud() {  # <label> <display-name> [extra args...]
  local label="$1" name="$2"; shift 2
  "$DD" --runtime cloud --repo "$CLONE" --task "${TASK:-$TDIR/task.md}" --model sonnet \
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
out_t="$(spawn_cloud cloud-timeout "$NAME_T" --timeout 1 2>&1)"; rc_t=$?
out_p="$(spawn_cloud cloud-probe "$NAME_P" 2>&1)"; rc_p=$?
[[ $rc_t -eq 0 && $rc_p -eq 0 ]] && ok "both cloud spawns exit 0" \
  || { bad "spawn rc t=$rc_t p=$rc_p"; printf '%s\n---\n%s\n' "$out_t" "$out_p"; }
[[ ! -s "$USAGE_GATE_LOG" ]] && ok "local usage gate never consulted (stub would refuse)" \
  || bad "usage gate was called for a cloud run"
[[ "$(cat "$DISPATCH/active-count.cloud" 2>/dev/null)" == 2 ]] && ok "active-count.cloud = 2" \
  || bad "active-count.cloud = $(cat "$DISPATCH/active-count.cloud" 2>/dev/null)"
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
row cloud-probe | grep -q "| $NAME_P \[DGN-1726\] |" && ok "ledger item = owner-facing name" \
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
board="$(DOGANY_LEDGER_FILE="$LEDGER" DOGANY_DISPATCH_DIR="$DISPATCH" DOGANY_LIVE_SHOW_MODEL=off \
  python3 - "$REPO/routines/status-footer.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sf", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for r in m._collect_ledger_running() or []:
    print(r)
PY
)"
echo "$board" | sed 's/^/    board: /'
grep -Eq "^Claude Sonnet 5\.5 \(Cloud\): $NAME_P \[DGN-1726\] · [0-9]{2}:[0-9]{2} \([0-9]+분~\)$" <<< "$board" \
  && ok "workbench row: Claude Sonnet 5.5 (Cloud): <name> · HH:MM (N분~) (model shown even with LIVE_SHOW_MODEL off)" \
  || bad "workbench row missing/wrong"

sleep $(( DOGANY_START_NOTICE_WINDOW + 3 ))
start_msg="$(awk '/^===MSG-END===$/{exit} {print}' "$PUSH_LOG")"
echo "$start_msg" | sed 's/^/    start: /'
grep -q "^▶️ .* 작업 시작 (2건)$" <<< "$start_msg" && ok "ONE batched start notice for the burst" \
  || bad "start notice title"
grep -Eq "^- Claude Sonnet 5\.5 \(Cloud\): $NAME_P [0-9]{2}:[0-9]{2}$" <<< "$start_msg" \
  && ok "start line: - Claude Sonnet 5.5 (Cloud): <name> HH:MM" || bad "start line for probe"
grep -q "^status --trigger-id trg_FAKE_cloud-probe --session-id $SID_P" "$FAKE_DIR/calls.log" \
  && ok "watcher polls the run status (trigger + session)" || bad "no status poll: $(cat "$FAKE_DIR/calls.log")"

echo "== (c) result branch appears -> completed =="
SHA_P="$(cloud_push cloud-probe)"
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
wait_for 5 grep -Eq "^- Claude Sonnet 5\.5 \(Cloud\): $NAME_P [0-9]{2}:[0-9]{2} \(" "$PUSH_LOG" \
  && ok "END notice line: - Claude Sonnet 5.5 (Cloud): <name> HH:MM (<elapsed>)" || bad "end notice missing"
grep -q "^✅ .* 작업 완료$" "$PUSH_LOG" && ok "END notice title" || bad "end notice title"
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
  && grep -q "푸시하지 않고 끝났다.*원인을 오너에게 한 줄로 알린" "$DROP_E" \
  && ok "dispatch-return: reason line, view URL, tell-the-owner directive" || { bad "ended drop"; cat "$DROP_E"; }
wait_for 5 grep -q "^⚠️ 작업실패: $NAME_E - " "$PUSH_LOG" && ok "owner END notice (작업실패) sent" || bad "ended notice"
grep "^⚠️ 작업실패: $NAME_E - " "$PUSH_LOG" | sed 's/^/    notice: /'

echo "== (e) routine create/run failure -> failed at once =="
NAME_S="루틴 생성 실패"
printf 'FAKE_START_FAIL\n' > "$TDIR/task-fail.md"
TASK="$TDIR/task-fail.md" spawn_cloud cloud-startfail "$NAME_S" >/dev/null 2>&1 || bad "startfail spawn rc"
RD_S="$(rundir_for cloud-startfail)"
wait_for 20 test -f "$RD_S/status.json" && ok "start failure finalized" || bad "start failure not finalized"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="error" and d["cloud_session"]=="" and d["cloud_fail_reason"]=="routine create/run failed: environment env_TEST not found (404)", d' \
  "$RD_S/status.json" 2>/dev/null && ok "status.json: error, no session, API reason" || bad "startfail status: $(cat "$RD_S/status.json" 2>/dev/null)"
grep -q "^disable --trigger-id trg_FAKE_cloud-startfail" "$FAKE_DIR/calls.log" \
  && ok "half-created routine disabled" || bad "no disable after failed run"
DROP_S="$(drop_for "$RD_S")"
wait_for 5 test -f "$DROP_S" && grep -q "^- cloud session: (start failed -- no session)$" "$DROP_S" \
  && grep -q "^- reason: routine create/run failed: environment env_TEST not found (404)$" "$DROP_S" \
  && grep -q "cloud 루틴 시작에 실패했다" "$DROP_S" && ok "dispatch-return: no session, reason, directive" \
  || { bad "startfail drop"; cat "$DROP_S" 2>/dev/null; }
wait_for 10 row_has cloud-startfail "| failed |.*cloud 실패 (session=none): routine create/run failed" \
  && ok "ledger -> failed with the reason" || bad "startfail ledger: $(row cloud-startfail)"
wait_for 5 grep -q "^⚠️ 작업실패: $NAME_S - " "$PUSH_LOG" && ok "owner END notice (작업실패) sent" || bad "startfail notice"

echo "== (j) task text mismatch after create -> never run, start failure =="
NAME_M="작업 문구 불일치"
printf 'FAKE_TASK_MISMATCH\n' > "$TDIR/task-mismatch.md"
TASK="$TDIR/task-mismatch.md" spawn_cloud cloud-mismatch "$NAME_M" >/dev/null 2>&1 || bad "mismatch spawn rc"
RD_M="$(rundir_for cloud-mismatch)"
wait_for 20 test -f "$RD_M/status.json" && ok "mismatch finalized at once" || bad "mismatch not finalized"
MM_REASON="task text mismatch after create (sha 111111111111 vs 222222222222)"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="error" and d["cloud_session"]=="" and d["cloud_branch_sha"]=="" and d["cloud_fail_reason"]==sys.argv[2], d' \
  "$RD_M/status.json" "$MM_REASON" 2>/dev/null && ok "status.json: error, no session, reason = the mismatch (not 'create/run failed')" \
  || bad "mismatch status: $(cat "$RD_M/status.json" 2>/dev/null)"
! grep -q "cloud-mismatch" "$FAKE_DIR/calls.log" | true
[[ "$(grep -c "trg_FAKE_cloud-mismatch" "$FAKE_DIR/calls.log")" == 0 ]] && ! grep -q "^status .*trg_FAKE_cloud-mismatch" "$FAKE_DIR/calls.log" \
  && ok "no run-status poll for the mismatched routine (nothing ran)" || bad "polled a mismatched routine"
DROP_M="$(drop_for "$RD_M")"
wait_for 5 test -f "$DROP_M" && grep -q "^- cloud session: (start failed -- no session)$" "$DROP_M" \
  && grep -q "^- reason: $MM_REASON$" "$DROP_M" && grep -q "^- routine: trg_FAKE_cloud-mismatch (disabled=yes)$" "$DROP_M" \
  && grep -q "cloud 루틴 시작에 실패했다" "$DROP_M" && ok "dispatch-return: start failure, reason = task text mismatch, routine disabled" \
  || { bad "mismatch drop"; cat "$DROP_M" 2>/dev/null; }
sed 's/^/    return: /' "$DROP_M" 2>/dev/null
wait_for 10 row_has cloud-mismatch "| failed |.*cloud 실패 (session=none): $MM_REASON" \
  && ok "ledger -> failed with the mismatch reason" || bad "mismatch ledger: $(row cloud-mismatch)"
wait_for 5 grep -q "^⚠️ 작업실패: $NAME_M - " "$PUSH_LOG" && ok "owner END notice (작업실패) sent" || bad "mismatch notice"

echo "== (g) --cancel: routine disabled, session link returned =="
NAME_C="취소 점검"
spawn_cloud cloud-cancel "$NAME_C" >/dev/null 2>&1 || bad "cancel spawn rc"
RD_C="$(rundir_for cloud-cancel)"
wait_for 20 grep -q '^CLOUD_SESSION_ID=cse' "$RD_C/cloud.env" || bad "cancel run never started"
# Pretend the post-run disable had failed so cancel must disable it itself.
sed -i '' 's/^CLOUD_TRIGGER_DISABLED=yes$/CLOUD_TRIGGER_DISABLED=no/' "$RD_C/cloud.env"
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
out="$("$DD" --runtime cloud --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --bottleneck-why "verifying a given parser change set" --label no-repo --display-name "저장소 없음" 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "requires --repo" <<< "$out" && ok "no --repo refused (rc 1)" || bad "no-repo: rc=$rc $out"
out="$(spawn_cloud wt-probe "워크트리" --worktree x 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "takes no --write/--worktree" <<< "$out" && ok "--worktree refused (rc 1)" || bad "worktree: rc=$rc $out"
out="$(DOGANY_DISPATCH_MAX_ACTIVE_CLOUD=1 spawn_cloud cap-probe "상한 점검" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "cap reached for runtime=cloud" <<< "$out" && ok "cloud cap enforced (1/1 -> rc 2, no fallback lane)" \
  || bad "cap: rc=$rc $out"
[[ "$(cat "$DISPATCH/active-count.cloud")" == 1 && "$(cat "$DISPATCH/active-count" 2>/dev/null || echo 0)" == 0 ]] \
  && ok "refused spawn moved no counter" || bad "counters after refusal"
printf 'AGENT_LANG=ko\n' > "$WS/config/agent.conf"
out="$(spawn_cloud no-env "환경 없음" 2>&1)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "set CLOUD_ENV_ID=env_... in .*config/agent.conf" <<< "$out" && ok "no CLOUD_ENV_ID refused with the key named (rc 1)" \
  || bad "no env: rc=$rc $out"
out="$(DOGANY_CLOUD_ENV_ID=env_OVERRIDE spawn_cloud no-env "환경 없음" --base nope-branch 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "\-\-base nope-branch is not on origin" <<< "$out" && ok "env override accepted; --base not on origin refused (rc 2)" \
  || bad "base: rc=$rc $out"
printf 'AGENT_LANG=ko\nCLOUD_ENV_ID=env_TEST01\n' > "$WS/config/agent.conf"
NG="$TDIR/nongh"
git clone -q "$ORIGIN" "$NG" 2>/dev/null
git -C "$NG" remote set-url origin "https://gitlab.com/acme/widgets.git"
out="$("$DD" --runtime cloud --repo "$NG" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --bottleneck-why "verifying a given parser change set" --label non-gh --display-name "깃랩" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && grep -q "not a GitHub repo (https://gitlab.com/acme/widgets.git)" <<< "$out" && ok "non-GitHub origin refused (rc 2)" \
  || bad "non-github: rc=$rc $out"

echo "== (f) no branch before --timeout -> timed-out =="
wait_for 90 test -f "$RD_T/status.json" && ok "timeout run finalized" || bad "timeout run never finalized"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["result"]=="timed-out" and d["exit_code"]==124 and d["cloud_branch_sha"]=="" and d["cloud_session"], d' \
  "$RD_T/status.json" 2>/dev/null && ok "status.json: timed-out, no sha, session kept" || bad "timeout status: $(cat "$RD_T/status.json")"
wait_for 10 row_has cloud-timeout "| failed |.*cloud timeout (1m 내 브랜치 미푸시" && ok "ledger -> failed (cloud timeout)" \
  || bad "timeout ledger: $(row cloud-timeout)"
DROP_T="$(drop_for "$RD_T")"
wait_for 5 test -f "$DROP_T" && ok "timeout dispatch-return dropped" || bad "no timeout drop"
grep -q "^- model: Claude Sonnet 5.5 (Cloud)$" "$DROP_T" && grep -q "^- branch: claude/cloud-timeout (not on origin)$" "$DROP_T" \
  && grep -q "^- reason: claude/cloud-timeout not on origin after 1m" "$DROP_T" \
  && grep -q "https://claude.ai/code/cse_FAKE_cloud-timeout 에서 상태를 확인" "$DROP_T" \
  && ok "timeout dispatch-return: model, branch absent, reason, view URL directive" || { bad "timeout drop"; cat "$DROP_T"; }
wait_for 5 grep -q "^⚠️ $NAME_T 작업이 멈췄습니다.$" "$PUSH_LOG" && ok "timeout -> existing stop notice" || bad "timeout notice"
[[ "$(cat "$DISPATCH/active-count.cloud")" == 0 ]] && ok "all cloud slots released" \
  || bad "active-count.cloud final: $(cat "$DISPATCH/active-count.cloud")"
[[ ! -s "$USAGE_GATE_LOG" ]] && ok "usage gate still never consulted" || bad "usage gate called"

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
python3 - <<'PY'
import json, os
a = os.environ["ACT"]
r = {"create": os.environ.get("FAKE_CREATE", ""),
     "get": os.environ.get("FAKE_GET_REPLY", "stored=" + os.environ.get("FAKE_STORED", "")),
     "run": os.environ.get("FAKE_RUN", ""),
     "list": os.environ.get("FAKE_LIST", "")}.get(a, os.environ.get("FAKE_REPLY", ""))
print(json.dumps({"is_error": False, "result": r}))
PY
EOF
chmod +x "$TDIR/bin/fake-claude-p"
CR="$REPO/routines/lib/cloud_routine.py"
export DOGANY_CLOUD_ROUTINE_CLAUDE="$TDIR/bin/fake-claude-p"
calls() { paste -sd, "$FAKE_DIR/helper-calls"; }
cr_start() {  # [task-file] -- one start against the fake; call log reset first
  : > "$FAKE_DIR/helper-calls"
  "$CR" start --name "dispatch x" --repo-url https://github.com/acme/widgets --env-id env_TEST01 \
    --model claude-opus-5-5 --task-file "${1:-$TDIR/task.md}" 2>/dev/null
}
json_of() {  # <file> -- the file's exact text as a JSON string literal
  python3 -c 'import json,sys; print(json.dumps(open(sys.argv[1], newline="").read(), ensure_ascii=False))' "$1"
}
export FAKE_CREATE='trigger_id=trig_01ABC' FAKE_STORED="$(json_of "$TDIR/task.md")" \
  FAKE_RUN=$'session_id=cse_01XYZ\nview=none\ndisabled=yes'
out="$(cr_start)"; rc=$?
[[ $rc -eq 0 && "$out" == $'trigger_id=trig_01ABC\nsession_id=cse_01XYZ\nview=https://claude.ai/code/cse_01XYZ\ndisabled=yes' ]] \
  && ok "start: stored text matches -> ids parsed, falls back to the claude.ai/code view URL" || bad "start parse: rc=$rc $out"
[[ "$(calls)" == "create,get,run" ]] && ok "start order: create -> get (verify) -> run" || bad "call order: $(calls)"
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
ccr = body["job_config"]["ccr"]
assert body["enabled"] is True
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
assert "action=run with trigger_id=trig_01ABC" in r and '{"enabled": false}' in r, r
PY
then ok "start: helper argv (sonnet, RemoteTrigger only, prompt on stdin) + create body shape; read-back prompt carries no task text"; else bad "helper argv/body"; fi
# Regression (dsp-20260926-203454-0567): a ~1900B task used to die at create
# with "local claude -p helper rc=1 ... Input must be provided ...".
python3 -c 'import sys; sys.stdout.write(("Review the dispatcher cloud lane. " * 56)[:1900] + "\n")' > "$TDIR/task-1900.md"
out="$(FAKE_CREATE='trigger_id=trig_01ABC' FAKE_STORED="$(json_of "$TDIR/task-1900.md")" \
  FAKE_RUN=$'session_id=cse_01XYZ\nview=none\ndisabled=yes' cr_start "$TDIR/task-1900.md")"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,get,run" ]] && ! grep -q "Input must be provided" <<< "$out" \
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
[[ "$(calls)" == "create,get,disable" ]] && ok "mismatch: the run call is NEVER made (create,get,disable)" || bad "mismatch calls: $(calls)"
out="$(FAKE_STORED="$MM_STORED" FAKE_REPLY='disabled=no' cr_start)"
grep -qx "disabled=no" <<< "$out" && grep -q "^reason=task text mismatch" <<< "$out" \
  && ok "mismatch + disable unconfirmed -> disabled=no reported (dispatcher retries the disable)" || bad "mismatch/no-disable: $out"
out="$(FAKE_GET_REPLY='error=trigger not found' FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,get,disable" ]] \
  && grep -q "^reason=task text unverified after create (get failed: trigger not found)" <<< "$out" \
  && ok "read-back error -> unverified, disabled, never run" || bad "get error: rc=$rc $out"
out="$(FAKE_GET_REPLY='stored=not-json' FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,get,disable" ]] \
  && grep -q "^reason=task text unverified after create (stored content is not a JSON string)" <<< "$out" \
  && ok "garbled read-back -> unverified, disabled, never run" || bad "garbled: rc=$rc $out"
printf 'caf\xc3\xa9 "quoted" \\ back\r\nline2\n\n' > "$TDIR/task-odd.md"
out="$(FAKE_STORED="$(json_of "$TDIR/task-odd.md")" cr_start "$TDIR/task-odd.md")"; rc=$?
[[ $rc -eq 0 && "$(calls)" == "create,get,run" ]] \
  && ok "byte-exact: non-ASCII, quotes, backslash, CRLF, trailing blank line all round-trip" || bad "odd text: rc=$rc $out"
out="$(FAKE_STORED="$(json_of "$TDIR/task-odd.md")" cr_start "$TDIR/task.md")"; rc=$?
[[ $rc -eq 1 ]] && grep -q "^reason=task text mismatch after create" <<< "$out" \
  && ok "a different stored text is a mismatch" || bad "different text: rc=$rc $out"
out="$(FAKE_CREATE='error=repository not authorized' cr_start)"; rc=$?
[[ $rc -eq 1 && "$out" == "error=repository not authorized" && "$(calls)" == "create" ]] \
  && ok "create API error -> rc 1, error line, nothing else called" || bad "create error: rc=$rc $out"
out="$(FAKE_RUN=$'error=quota exceeded\ndisabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$out" == $'trigger_id=trig_01ABC\ndisabled=yes\nerror=quota exceeded' ]] \
  && ok "run API error after a verified create -> rc 1, trigger id + disabled + error" || bad "run error: rc=$rc $out"
out="$(FAKE_HELPER_ERROR=boom FAKE_HELPER_ERROR_ON=create cr_start)"; rc=$?
[[ $rc -eq 1 ]] && grep -q "^error=local claude -p helper error: boom$" <<< "$out" && ok "create helper failure -> rc 1 with error" \
  || bad "helper failure: rc=$rc $out"
out="$(FAKE_HELPER_ERROR=boom FAKE_HELPER_ERROR_ON=run FAKE_REPLY='disabled=yes' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,get,run,disable" ]] \
  && grep -qx "trigger_id=trig_01ABC" <<< "$out" && grep -q "^reason=routine run call failed: local claude -p helper error: boom" <<< "$out" \
  && ok "run-call helper failure -> routine disabled, trigger id + reason reported" || bad "run helper failure: rc=$rc $out"
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
[[ $rc -eq 0 && "$(calls)" == "create,list,get,run" ]] && grep -qx "trigger_id=trig_X1" <<< "$out" \
  && grep -q "action=get with trigger_id=trig_X1" "$FAKE_DIR/helper-prompt.get" \
  && grep -q '"dispatch x"' "$FAKE_DIR/helper-prompt.list" \
  && ok "helper reply trg_X1 + list trig_X1 -> emitted trigger_id=trig_X1, get/run use it" || bad "re-resolve: rc=$rc calls=$(calls) $out"
out="$(FAKE_CREATE='trigger_id=trig_X1' cr_start)"
[[ "$(calls)" == "create,get,run" ]] && ok "valid trig_ id -> no list call" || bad "valid id list call: $(calls)"
out="$(FAKE_CREATE='trigger_id=trg_X1' FAKE_LIST='error=no routine named that' cr_start)"; rc=$?
[[ $rc -eq 1 && "$(calls)" == "create,list" ]] && grep -q "^error=trigger id 'trg_X1' is not a trig_ id" <<< "$out" \
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
if [[ "$prompt" == *"action=list."* ]]; then r='trigger_id=trig_X3'
elif [[ "$prompt" == *"trigger_id=trig_X3"* ]]; then r='disabled=yes'
else r='error=HTTP 400 invalid trigger ID format'; fi
echo "$prompt" | grep -o 'action=[a-z]* ' | head -1 >> "$FAKE_DIR/helper-calls"
python3 -c 'import json,sys; print(json.dumps({"is_error": False, "result": sys.argv[1]}))' "$r"
EOS
chmod +x "$TDIR/bin/fake-claude-2"
out="$(DOGANY_CLOUD_ROUTINE_CLAUDE="$TDIR/bin/fake-claude-2" "$CR" disable --trigger-id trg_X3 --name "dispatch x")"; rc=$?
[[ $rc -eq 0 && "$out" == $'trigger_id=trig_X3\ndisabled=yes' ]] && ok "disable: trg_X3 invalid -> list -> disabled with trig_X3" || bad "disable resolve: rc=$rc $out"
unset FAKE_STORED FAKE_RUN FAKE_CREATE FAKE_LIST FAKE_GET_REPLY

echo
echo "== DGN-1726 cloud runtime: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
