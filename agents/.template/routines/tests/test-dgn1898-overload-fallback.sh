#!/usr/bin/env bash
# test-lane: slow
unset DOGANY_BRIDGE_SESSION  # DGN-1688: hermetic -- never look like a self-cancel
# test-dgn1898-overload-fallback.sh -- DGN-1898: a claude-code dispatch that
# dies on API 529 (overloaded) before its first tool call is re-run on the
# next model of the chain (fable, then sonnet at effort max), recorded on the
# ledger row, run.env, status.json and the session-inbox drop. Never silent,
# never after the worker has started working.
#
# Evidence: 2026-10-06 21:18 dsp-20261006-211850-1110 (dgn-1895-262fin, opus)
# died with 7x api_retry error_status=529 then "API Error: 529 Overloaded",
# zero tool use; the dispatcher finalized it as error and nothing re-ran it.
# fixtures/dgn1898/opus-529-stream.jsonl is that stream (init + rate-limit
# lines dropped: they carry host paths and nothing this test reads).
#
# Run: bash routines/tests/test-dgn1898-overload-fallback.sh
# Safe/offline: fake claude via PATH, push/usage-gate stubs, scratch repo.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"
FIX529="$HARNESS_DIR/fixtures/dgn1898/opus-529-stream.jsonl"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1898-test.XXXXXX)"
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
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit 0
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:?}"
exit 0
EOF
# Fake claude. Per --model behaviour from env:
#   FAKE_529_MODELS     comma list: replay the real 529 death stream, exit 1
#   FAKE_TOOL529_MODELS comma list: one tool_use block, THEN the 529 death
#   FAKE_CREDITS_MODELS comma list: fable weekly-cap death text, exit 1
#   FAKE_PLAIN_FAIL     any model: plain error text, exit 1
# Every call appends "model effort session-flag" to $FAKE_CALLS. With
# --session-id it creates the CLI transcript the way the real CLI does, so a
# re-use of the same id is refused exactly like the real CLI (verified on
# 2.1.289: "Session ID ... is already in use.").
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
model="" effort="-" sess="none" sid=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    --model) model="${args[$((i+1))]}" ;;
    --effort) effort="${args[$((i+1))]}" ;;
    --session-id) sess="session-id"; sid="${args[$((i+1))]}" ;;
    --resume) sess="resume"; sid="${args[$((i+1))]}" ;;
  esac
done
echo "$model $effort $sess" >> "${FAKE_CALLS:?}"
if [[ -n "$sid" ]]; then
  pdir="$HOME/.claude/projects/$(pwd -P | sed 's/[^A-Za-z0-9]/-/g')"
  if [[ "$sess" == "session-id" && -f "$pdir/$sid.jsonl" ]]; then
    echo "Error: Session ID $sid is already in use." >&2; exit 1
  fi
  if [[ "$sess" == "resume" && ! -f "$pdir/$sid.jsonl" ]]; then
    echo "No conversation found with session ID: $sid" >&2; exit 1
  fi
  mkdir -p "$pdir" && echo '{}' >> "$pdir/$sid.jsonl"
fi
inlist() { [[ ",$1," == *",$model,"* ]]; }
if inlist "${FAKE_TOOL529_MODELS:-}"; then
  echo '{"type":"assistant","message":{"model":"claude-x","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}}]}}'
  cat "$FAKE_529_STREAM"; exit 1
fi
if inlist "${FAKE_529_MODELS:-}"; then cat "$FAKE_529_STREAM"; exit 1; fi
if [[ "$model" == fable ]] && inlist "${FAKE_CREDITS_MODELS:-}"; then
  echo '{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Fable 5 requires usage credits. Switch to another model, ..."}]}}'
  exit 1
fi
if [[ -n "${FAKE_PLAIN_FAIL:-}" ]]; then
  echo '{"type":"assistant","message":{"model":"claude-x","content":[{"type":"text","text":"something else broke"}]}}'
  exit 1
fi
echo "{\"type\":\"assistant\",\"message\":{\"model\":\"claude-$model-fake\",\"content\":[{\"type\":\"text\",\"text\":\"WORK-DONE by $model\"}]}}"
exit 0
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF
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
export FAKE_529_STREAM="$FIX529"
export HOME="$TDIR/home"   # fake CLI transcripts land here, never in ~/.claude
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
ledger_note() { grep "| $1 |" "$WS/product/auto-loop-ledger.md" | awk -F'|' '{print $9}'; }

# spawn <case> <model> [extra args...] -> sets RUNDIR, CALLS
spawn() {
  local c="$1" m="$2"; shift 2
  CALLS="$TDIR/calls-$c.txt"; : > "$CALLS"
  local out
  out="$(FAKE_CALLS="$CALLS" "$DD" --task "$TDIR/task.md" --model "$m" --bottleneck accuracy \
    --bottleneck-why "given checks need exact pass fail verification" --why "dgn1898 test stub run" \
    --label "ovl $c" --timeout 2 --notify silent "$@" 2>&1)"
  RUNDIR="$WS/.telegram_bot/dispatch/$(runid_of "$out")"
  if ! wait_finalized "$RUNDIR" 60; then
    bad "$c: run did not finalize ($out)"
    return 1
  fi
}

echo "== 1. predicate on the real 2026-10-06 529 stream =="
(
  cd "$TDIR"
  export DISPATCH_TEST=1
  # shellcheck disable=SC1090
  source "$DD"
  RUNTIME=claude-code
  overload_death_kind "$FIX529"; echo "K1=$(overload_death_kind "$FIX529")"
  { echo '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t","name":"Bash","input":{}}]}}'; cat "$FIX529"; } > "$TDIR/tool529.jsonl"
  echo "K2=$(overload_death_kind "$TDIR/tool529.jsonl")"
  echo '{"type":"result","is_error":true,"api_error_status":500,"result":"API Error: 500"}' > "$TDIR/e500.jsonl"
  echo "K3=$(overload_death_kind "$TDIR/e500.jsonl")"
  echo "K4=$(overload_death_kind "$TDIR/missing.jsonl")"
) > "$TDIR/t1.out" 2>&1
grep -q "^K1=overloaded$" "$TDIR/t1.out" && ok "P1 real 529 stream, zero tool use -> overloaded" || bad "P1 real 529 stream ($(cat "$TDIR/t1.out" | head -3))"
grep -q "^K2=$" "$TDIR/t1.out" && ok "P2 529 after a tool call -> not a fallback case" || bad "P2 tool-use 529"
grep -q "^K3=$" "$TDIR/t1.out" && ok "P3 non-529 API error -> not a fallback case" || bad "P3 500"
grep -q "^K4=$" "$TDIR/t1.out" && ok "P4 missing stream -> not a fallback case" || bad "P4 missing"

echo "== 2. opus 529 (worktree) -> fable re-run, recorded =="
if FAKE_529_MODELS=opus spawn wt-a opus --worktree ovla; then
  [[ "$(cat "$CALLS")" == $'opus medium none\nfable medium none' ]] \
    && ok "A1 calls: opus, then fable with the same brief/effort" || bad "A1 calls: $(tr '\n' ';' < "$CALLS")"
  [[ "$(sfield "$RUNDIR/status.json" result)" == completed ]] && ok "A2 run completed on the fallback" || bad "A2 result=$(sfield "$RUNDIR/status.json" result)"
  [[ "$(sfield "$RUNDIR/status.json" model)" == fable && "$(sfield "$RUNDIR/status.json" model_fallback_from)" == opus ]] \
    && ok "A3 status.json model=fable model_fallback_from=opus" || bad "A3 status.json model/from"
  n="$(sfield "$RUNDIR/status.json" model_fallback_note)"
  [[ "$n" == *"opus"*"529"*"fable"* ]] && ok "A4 status.json fallback note names opus 529 -> fable" || bad "A4 note: $n"
  [[ "$n" == *"binding"* ]] && ok "A5 off-binding fallback (accuracy axis binds sonnet,opus) carries the auto reason" || bad "A5 note: $n"
  ln="$(ledger_note dsp/ovla)"
  [[ "$ln" == *"model_fallback=opus->fable"* ]] \
    && ok "A6 ledger end row keeps the hop (model_fallback=opus->fable)" || bad "A6 ledger: $ln"
  grep -q "model fallback: opus 529-overloaded, 0 tool use -> fable" "$RUNDIR/worker.log" \
    && ok "A6b worker.log carries the hop line (running-row rewrite path ran)" || bad "A6b worker.log: $(tail -5 "$RUNDIR/worker.log")"
  ( source "$RUNDIR/run.env"; [[ "$MODEL" == fable && "$MODEL_FALLBACK_FROM" == opus ]] ) \
    && ok "A7 run.env (sourced) carries the switched model" || bad "A7 run.env"
  ls "$RUNDIR"/stream.attempt1-opus.jsonl >/dev/null 2>&1 \
    && ok "A8 the 529 attempt's stream is kept as evidence" || bad "A8 archived stream missing: $(ls "$RUNDIR")"
  grep -q "WORK-DONE by fable" "$RUNDIR/out.log" && ok "A9 out.log is the fallback run's report" || bad "A9 out.log"
fi

echo "== 3. opus 529, fable flagged exhausted -> sonnet at effort max =="
python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)+d.timedelta(days=1)).isoformat()}, open('$WS/.claude/.fable-exhausted','w'))"
if FAKE_529_MODELS=opus spawn wt-b opus --worktree ovlb; then
  [[ "$(cat "$CALLS")" == $'opus medium none\nsonnet max none' ]] \
    && ok "B1 calls: opus, then sonnet --effort max (fable skipped)" || bad "B1 calls: $(tr '\n' ';' < "$CALLS")"
  [[ "$(sfield "$RUNDIR/status.json" model)" == sonnet && "$(sfield "$RUNDIR/status.json" effort)" == max \
     && "$(sfield "$RUNDIR/status.json" result)" == completed ]] \
    && ok "B2 status.json model=sonnet effort=max completed" || bad "B2 status.json"
fi
rm -f "$WS/.claude/.fable-exhausted"

echo "== 4. opus 529, fable 529 -> sonnet max =="
if FAKE_529_MODELS=opus,fable spawn wt-c opus --worktree ovlc; then
  [[ "$(cat "$CALLS")" == $'opus medium none\nfable medium none\nsonnet max none' ]] \
    && ok "C1 calls: opus, fable, sonnet max" || bad "C1 calls: $(tr '\n' ';' < "$CALLS")"
  n="$(sfield "$RUNDIR/status.json" model_fallback_note)"
  [[ "$n" == *"fable"*"sonnet"* ]] && ok "C2 note records both hops" || bad "C2 note: $n"
fi

echo "== 5. opus 529, fable refused (weekly cap) -> sonnet max, fable flag written =="
if FAKE_529_MODELS=opus FAKE_CREDITS_MODELS=fable spawn wt-d opus --worktree ovld; then
  [[ "$(cat "$CALLS")" == $'opus medium none\nfable medium none\nsonnet max none' ]] \
    && ok "D1 calls: opus, fable, sonnet max" || bad "D1 calls: $(tr '\n' ';' < "$CALLS")"
  [[ -f "$WS/.claude/.fable-exhausted" ]] && ok "D2 fable weekly-cap flag recorded (DGN-1158)" || bad "D2 flag"
  [[ "$(sfield "$RUNDIR/status.json" result)" == completed ]] && ok "D3 completed on sonnet" || bad "D3 result"
fi
rm -f "$WS/.claude/.fable-exhausted"

echo "== 6. no fallback when the worker had started working / other failures =="
if FAKE_TOOL529_MODELS=opus spawn wt-e opus --worktree ovle; then
  [[ "$(cat "$CALLS")" == 'opus medium none' && "$(sfield "$RUNDIR/status.json" result)" == error ]] \
    && ok "E1 529 after a tool call: no re-run, error" || bad "E1 calls: $(tr '\n' ';' < "$CALLS")"
  [[ -z "$(sfield "$RUNDIR/status.json" model_fallback_from)" ]] && ok "E2 no fallback recorded" || bad "E2"
fi
if FAKE_PLAIN_FAIL=1 spawn wt-f opus --worktree ovlf; then
  [[ "$(cat "$CALLS")" == 'opus medium none' ]] && ok "E3 non-529 failure: no re-run" || bad "E3 calls: $(tr '\n' ';' < "$CALLS")"
fi
if FAKE_529_MODELS=sonnet spawn wt-g sonnet --worktree ovlg; then
  [[ "$(cat "$CALLS")" == 'sonnet low none' && "$(sfield "$RUNDIR/status.json" result)" == error ]] \
    && ok "E4 sonnet 529: end of chain, error" || bad "E4 calls: $(tr '\n' ';' < "$CALLS")"
fi
if FAKE_529_MODELS=opus,fable,sonnet spawn wt-h opus --worktree ovlh; then
  [[ "$(cat "$CALLS")" == $'opus medium none\nfable medium none\nsonnet max none' \
     && "$(sfield "$RUNDIR/status.json" result)" == error ]] \
    && ok "E5 every hop 529: three attempts max, then error" || bad "E5 calls: $(tr '\n' ';' < "$CALLS")"
  [[ "$(sfield "$RUNDIR/status.json" model_fallback_note)" == *"sonnet 529-overloaded, 0 tool use -> end of fallback chain"* ]] \
    && ok "E6 failed chain still recorded, end of chain named" || bad "E6 $(sfield "$RUNDIR/status.json" model_fallback_note)"
  [[ "$(ledger_note dsp/ovlh)" == *"model_fallback=opus->sonnet"* ]] && ok "E7 failed end row keeps the hop" || bad "E7 $(ledger_note dsp/ovlh)"
  grep -q "API Error: 529" "$RUNDIR/out.log" && ok "E8 out.log of a dead chain still shows the last death" || bad "E8 out.log"
fi

echo "== 7. observe mode: the retry resumes the sandbox-bound session id =="
if FAKE_529_MODELS=opus spawn ob-a opus --read-only; then
  [[ "$(cat "$CALLS")" == $'opus medium session-id\nfable medium resume' ]] \
    && ok "O1 calls: opus --session-id, then fable --resume <same id>" || bad "O1 calls: $(tr '\n' ';' < "$CALLS")"
  [[ "$(sfield "$RUNDIR/status.json" result)" == completed ]] && ok "O2 observe fallback completed" || bad "O2 result=$(sfield "$RUNDIR/status.json" result) $(cat "$RUNDIR/out.log" | head -5)"
fi

COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo missing)"
[[ "$COUNT" == "0" ]] && ok "Z active-count back to 0" || bad "Z active-count $COUNT"

echo
echo "dgn1898 overload fallback: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
