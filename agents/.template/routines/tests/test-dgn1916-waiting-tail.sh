#!/usr/bin/env bash
# test-lane: slow
unset DOGANY_BRIDGE_SESSION  # DGN-1688: hermetic -- never look like a self-cancel
# test-dgn1916-waiting-tail.sh -- DGN-1916: a detached claude -p worker that
# backgrounds its test lane and ends its turn "waiting" exits 0 with no
# result. The return gate classifies such a run INCOMPLETE (never completed),
# and the worker backstop forbids backgrounding inside a detached run.
#
# Evidence: 2026-10-07 dsp-20261007-122645-8353 (WT-2) out.log ended
# "Re-running the 11 failing suites ... waiting for that to finish";
# dsp-20261007-133005-2223 ended "Fast lane running; I'll be notified when it
# finishes." Both exit 0, both recorded completed with no report.
#
# Run: bash routines/tests/test-dgn1916-waiting-tail.sh
# Safe/offline: fake claude via PATH, push/usage-gate stubs, scratch repo.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1916-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
for f in ledger-update.sh dispatch-tier-bindings.tsv dispatch-effort-bindings.tsv \
         model_display.py dispatch-model-display.tsv; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
cat > "$WS/routines/usage-gate.sh" <<'EOS'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit 0
EOS
cat > "$WS/routines/push.sh" <<'EOS'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:?}"
exit 0
EOS
# Fake claude: one tool call, then the final assistant text from $FAKE_TEXT,
# exit 0 -- exactly what the 2026-10-07 runs looked like from outside. The
# --append-system-prompt value (the backstop) is saved to $FAKE_BACKSTOP.
cat > "$TDIR/bin/claude" <<'EOS'
#!/bin/bash
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [[ "${args[$i]}" == --append-system-prompt ]] && printf '%s' "${args[$((i+1))]}" > "${FAKE_BACKSTOP:?}"
done
echo '{"type":"assistant","message":{"model":"claude-x","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"bash git-hooks/run-tests.sh","run_in_background":true}}]}}'
python3 -c 'import json,os; print(json.dumps({"type":"assistant","message":{"model":"claude-x","content":[{"type":"text","text":os.environ["FAKE_TEXT"]}]}}))'
exit 0
EOS
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"
cat > "$WS/product/auto-loop-ledger.md" <<'EOS'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOS
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
export PUSH_STUB_LOG="$TDIR/push-calls.log"
export FAKE_BACKSTOP="$TDIR/backstop.txt"
export HOME="$TDIR/home"
mkdir -p "$HOME"
DD="$WS/routines/dispatch-detached.sh"
printf 'do the task\n' > "$TDIR/task.md"

wait_finalized() {
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" && -f "$1/status.json" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1"; }
sfield() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2],""); print(v if isinstance(v,str) else json.dumps(v))' "$1" "$2"; }
ledger_row() { grep "| $1 |" "$WS/product/auto-loop-ledger.md"; }

# spawn <case> <final text> [extra args...] -> sets RUNDIR, RUNID
spawn() {
  local c="$1" text="$2"; shift 2
  local out
  out="$(FAKE_TEXT="$text" "$DD" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
    --bottleneck-why "given checks need exact pass fail verification" --why "dgn1916 test stub run" \
    --label "wait $c" --timeout 2 --notify silent "$@" 2>&1)"
  RUNID="$(runid_of "$out")"
  RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
  if ! wait_finalized "$RUNDIR" 60; then
    bad "$c: run did not finalize ($out)"
    return 1
  fi
}

echo "== 1. predicate: last line of out.log =="
(
  cd "$TDIR"
  export DISPATCH_TEST=1
  # shellcheck disable=SC1090
  source "$DD"
  t() { printf '%s\n' "$2" > "$TDIR/p.log"; if out_log_waiting_tail "$TDIR/p.log" >/dev/null; then echo "$1=wait"; else echo "$1=done"; fi; }
  t W1 $'Committed the fix.\n\nRe-running the 11 failing suites ... waiting for that to finish'
  t W2 $'Fast lane running; I\'ll be notified when it finishes.'
  t W3 $'테스트를 백그라운드로 돌려 두었습니다. 끝나면 알려드릴게요.'
  t W4 $'Started the slow lane in the background.'
  t W5 $'Waiting on the full suite.'
  t D1 $'Waiting for the suite was slow, so I split it.\n\nAll 14 suites pass. branch: dsp/x  sha: abc1234'
  t D2 $'## Result\n- fixed parser\n- tests: 12/12 green'
  t D3 $'Done.\n\n--- stderr ---\nwaiting for lock'
  printf '' > "$TDIR/empty.log"
  out_log_waiting_tail "$TDIR/empty.log" >/dev/null && echo "D4=wait" || echo "D4=done"
  out_log_waiting_tail "$TDIR/missing.log" >/dev/null && echo "D5=wait" || echo "D5=done"
  printf 'x\nwaiting for that to finish\n' > "$TDIR/p.log"
  echo "L=$(out_log_waiting_tail "$TDIR/p.log")"
) > "$TDIR/t1.out" 2>&1
for k in W1 W2 W3 W4 W5; do
  grep -q "^$k=wait$" "$TDIR/t1.out" && ok "P $k waiting tail -> incomplete" || bad "P $k ($(grep "^$k=" "$TDIR/t1.out"))"
done
for k in D1 D2 D3 D4 D5; do
  grep -q "^$k=done$" "$TDIR/t1.out" && ok "P $k report tail -> not waiting" || bad "P $k ($(grep "^$k=" "$TDIR/t1.out"))"
done
grep -q "^L=waiting for that to finish$" "$TDIR/t1.out" && ok "P the matched line is printed" || bad "P line: $(grep '^L=' "$TDIR/t1.out")"

echo "== 2. worker ends 'waiting' (exit 0) -> incomplete, not completed =="
if spawn wa "Re-running the 11 failing suites ... waiting for that to finish" --worktree waita; then
  [[ "$(sfield "$RUNDIR/status.json" result)" == incomplete ]] \
    && ok "A1 status.json result=incomplete" || bad "A1 result=$(sfield "$RUNDIR/status.json" result)"
  [[ "$(sfield "$RUNDIR/status.json" exit_code)" == 0 ]] && ok "A2 exit code stays 0 (lifecycle fact kept)" || bad "A2 exit=$(sfield "$RUNDIR/status.json" exit_code)"
  row="$(ledger_row dsp/waita)"
  [[ "$row" == *"| failed |"* && "$row" == *"DGN-1916"* ]] \
    && ok "A3 ledger end row: failed, names DGN-1916" || bad "A3 ledger: $row"
  grep -q "DGN-1916: incomplete" "$RUNDIR/out.log" && ok "A4 out.log carries the dispatcher's incomplete line" || bad "A4 out.log: $(cat "$RUNDIR/out.log")"
  ib="$WS/.telegram_bot/session-inbox/dispatch-$RUNID.md"
  if [[ -f "$ib" ]]; then
    grep -q -- "-- incomplete" "$ib" && ok "A5 session-inbox drop says incomplete" || bad "A5 inbox head: $(head -1 "$ib")"
    grep -q "대기 문구로 끝나 최종 보고가 없다" "$ib" && ok "A6 inbox directive names the missing report" || bad "A6 inbox: $(tail -3 "$ib")"
  else
    bad "A5 no session-inbox drop at $ib"
  fi
  grep -q "DGN-1916" "$RUNDIR/worker.log" && ok "A7 worker.log records the classification" || bad "A7 worker.log"
fi

echo "== 3. second observed shape, observe mode =="
if spawn wb "Fast lane running; I'll be notified when it finishes." --read-only; then
  [[ "$(sfield "$RUNDIR/status.json" result)" == incomplete ]] \
    && ok "B1 observe run ending on 'I'll be notified' -> incomplete" || bad "B1 result=$(sfield "$RUNDIR/status.json" result) $(head -5 "$RUNDIR/out.log")"
fi

echo "== 4. a real report still completes =="
if spawn wc $'All suites pass (12/12).\nbranch: dsp/waitc' --worktree waitc; then
  [[ "$(sfield "$RUNDIR/status.json" result)" == completed ]] \
    && ok "C1 report tail -> completed" || bad "C1 result=$(sfield "$RUNDIR/status.json" result)"
  grep -q "DGN-1916" "$RUNDIR/out.log" && bad "C2 out.log got an incomplete line" || ok "C2 out.log untouched"
fi

echo "== 5. backstop forbids backgrounding inside a detached run =="
if [[ -f "$FAKE_BACKSTOP" ]]; then
  grep -q "NEVER background a command" "$FAKE_BACKSTOP" && ok "S1 backstop: never background" || bad "S1"
  grep -q "run_in_background" "$FAKE_BACKSTOP" && ok "S2 backstop names run_in_background" || bad "S2"
  grep -q "FOREGROUND" "$FAKE_BACKSTOP" && grep -q "split it" "$FAKE_BACKSTOP" \
    && ok "S3 backstop: foreground, split over the tool timeout" || bad "S3"
  grep -q "recorded INCOMPLETE" "$FAKE_BACKSTOP" && ok "S4 backstop states the return gate" || bad "S4"
else
  bad "S0 fake claude never received a backstop"
fi

COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo missing)"
[[ "$COUNT" == "0" ]] && ok "Z active-count back to 0" || bad "Z active-count $COUNT"

echo
echo "dgn1916 waiting tail: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
