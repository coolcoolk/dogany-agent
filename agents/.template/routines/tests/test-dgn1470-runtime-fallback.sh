#!/usr/bin/env bash
# test-lane: fast
# test-dgn1470-runtime-fallback.sh -- bidirectional runtime fallback +
# pre-spawn decision records (DGN-1470; design: files/reports/
# runtime-fallback-20260915.md).
#
# Covers (fixture-injected, NO real dispatch / network / launchd):
#   T1  claude usage-gate rc=1 (quota tight) + observe (read lane) + codex
#       quota healthy -> automatic claude-code->codex fallback, recorded.
#   T2  claude rc=1 + WRITE (worktree) + hook trust unverified at target ->
#       NO fallback; bounded defer with a persisted reservation. This is the
#       2026-09-15 11:35 shape (claude 95% / codex 64% / canonical-repo
#       write): unconditional fallback here would be a design violation.
#   T3  claude rc=2 (lookup failed) -> no automatic fallback (DGN-1590); and
#       rc=2 with codex ALSO unreadable -> defer on the fixed retry interval.
#   T4  codex over-ceiling / read-failure -> reverse switch to claude-code,
#       recorded (the old code only switched on read failure, silently).
#   T5  both lanes blocked with the defer budget exhausted -> reasoned
#       closure (expired), never an unbounded wait; reservation write
#       failure -> blocked, never "waiting".
#   T6  every refusal/fallback/defer decision above leaves a JSON record
#       under .telegram_bot/dispatch/decisions/ (spawn-preceding, outside
#       any rundir).
#   T7  no-regression: default claude path with a passing gate creates NO
#       decision file and switches nothing; unmeasured third runtime passes
#       through untouched; one-switch-max (never a claude<->codex loop).
#   (T8 = the existing dispatch suites, run separately.)
#
# Run: bash routines/tests/test-dgn1470-runtime-fallback.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1470-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() { rm -rf "$TDIR"; }
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/.telegram_bot" "$WS/.claude"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/model_display.py" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-model-display.tsv" "$WS/routines/lib/"

# usage-gate.sh stub: rc + trigger line from env (matches the real contract:
# rc=1 tight, rc=2 lookup-failed, last stdout line carries trigger=).
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
rc="${GATE_STUB_RC:-0}"
case "$rc" in
  0) echo "USAGE_GATE: pass five_hour=${GATE_STUB_5H:-10} seven_day=${GATE_STUB_7D:-10}" ;;
  1) echo "USAGE_GATE: defer reason=tight five_hour=${GATE_STUB_5H:-95} seven_day=${GATE_STUB_7D:-64} trigger=${GATE_STUB_TRIGGER:-five_hour}" ;;
  *) echo "USAGE_GATE: defer reason=lookup-failed" ;;
esac
exit "$rc"
EOF

# claude-usage.sh stub: --json with utilization + resets_at (epoch via env).
cat > "$WS/routines/claude-usage.sh" <<'EOF'
#!/bin/bash
if [[ "${CLAUDE_USAGE_STUB_FAIL:-}" == "1" ]]; then
  echo "stub: simulated read failure" >&2
  exit 1
fi
python3 - <<'PY'
import json, os, time
def iso(e): return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(e)))
now = int(time.time())
print(json.dumps({
  "five_hour": {"utilization": int(os.environ.get("STUB_FIVE_HOUR_UTIL", "10")),
                 "resets_at": iso(os.environ.get("STUB_5H_RESET", now + 3600))},
  "seven_day": {"utilization": int(os.environ.get("STUB_SEVEN_DAY_UTIL", "10")),
                 "resets_at": iso(os.environ.get("STUB_7D_RESET", now + 86400))},
}))
PY
EOF

# codex-ratelimit.py stub: quota read (env) + --record-failure sink.
cat > "$WS/routines/codex-ratelimit.py" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
if "--record-failure" in sys.argv:
    sys.exit(0)
if os.environ.get("CODEX_RATELIMIT_STUB_FAIL") == "1":
    print(json.dumps({"ok": False, "kind": "lookup-failed", "raw": "stub", "elapsed_ms": 5}))
    sys.exit(2)
print(json.dumps({"ok": True, "used_percent": int(os.environ.get("STUB_CODEX_USED", "10"))}))
sys.exit(0)
EOF

# codex-hook-verify.py stub: trust verdict from env.
cat > "$WS/routines/codex-hook-verify.py" <<'EOF'
#!/usr/bin/env python3
import os, sys
if os.environ.get("STUB_HOOK_OK") == "1":
    print("stub: hooks trusted")
    sys.exit(0)
print("stub: no persisted hook trust")
sys.exit(2)
EOF

# usage-defer-retry.sh stub: records the reservation argv, rc from env.
cat > "$WS/routines/usage-defer-retry.sh" <<'EOF'
#!/bin/bash
printf '%s\n' "DEFER-STUB $*" >> "${DEFER_STUB_LOG:?}"
exit "${DEFER_STUB_RC:-0}"
EOF

chmod +x "$WS"/routines/*.sh "$WS"/routines/*.py
DD="$WS/routines/dispatch-detached.sh"
DECDIR="$WS/.telegram_bot/dispatch/decisions"
export DEFER_STUB_LOG="$TDIR/defer-calls.log"
printf 'fixture task\n' > "$TDIR/task.md"
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_DISPATCH_RUNTIME 2>/dev/null || true

run_source() {
  # $1 = snippet evaluated after sourcing (clean subshell, cleared argv).
  local snippet="$1"
  (
    cd "$TDIR"
    export DISPATCH_TEST=1
    set --
    # shellcheck disable=SC1090
    source "$DD"
    set +e
    TASK_FILE="$TDIR/task.md"
    # DGN-1750 (2026-09-27): codex has no judgment row any more (judgment stays
    # on claude), so the fallback fixture uses an axis both runtimes bind.
    MODEL=opus
    BOTTLENECK=coverage
    LABEL=dgn1470-fixture
    DISPLAY_NAME=fixture
    TICKET=DGN-1470
    TIMEOUT_MIN=30
    NOTIFY=silent
    eval "$snippet"
  )
}

reset_counts() {
  rm -rf "$WS/.telegram_bot/dispatch" 2>/dev/null || true
  : > "$DEFER_STUB_LOG"
}

latest_decision() {
  ls -t "$DECDIR"/rtd-*.json 2>/dev/null | grep -v prompt | head -1
}

dec_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2" 2>/dev/null
}

# ---------------------------------------------------------------------------
echo "== T1: claude rc=1 (tight) + observe + codex healthy -> fallback =="
reset_counts
OUT="$(GATE_STUB_RC=1 GATE_STUB_5H=95 GATE_STUB_7D=64 STUB_CODEX_USED=64 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=$RUNTIME_FALLBACK_DIRECTION REQ=$REQUESTED_RUNTIME"
  echo "CODEX_SLOTS=$(read_active_count codex) CLAUDE_SLOTS=$(read_active_count claude-code)"
' 2>&1)"
grep -q "RC=0 RUNTIME=codex DIR=claude-code->codex REQ=auto" <<< "$OUT" \
  && ok "T1 rc=1 observe falls back to codex (direction recorded, request preserved)" \
  || bad "T1 fallback state ($OUT)"
grep -q "CODEX_SLOTS=1 CLAUDE_SLOTS=0" <<< "$OUT" \
  && ok "T1 slot charged to codex, never claude" || bad "T1 slot accounting ($OUT)"
DEC="$(latest_decision)"
if [[ -n "$DEC" && "$(dec_field "$DEC" status)" == "selected" \
   && "$(dec_field "$DEC" fallback_direction)" == "claude-code->codex" \
   && "$(dec_field "$DEC" requested_runtime)" == "auto" ]]; then
  ok "T1/T6 fallback decision persisted (status=selected)"
else
  bad "T1/T6 decision record ($DEC: $(cat "$DEC" 2>/dev/null))"
fi

# ---------------------------------------------------------------------------
echo "== T2/검산(11:35): claude 95% rc=1 + WRITE + no hook trust + codex 64% -> bounded defer, NO fallback =="
reset_counts
OUT="$(GATE_STUB_RC=1 GATE_STUB_5H=95 GATE_STUB_7D=64 STUB_CODEX_USED=64 STUB_HOOK_OK=0 \
  STUB_FIVE_HOUR_UTIL=95 STUB_5H_RESET=$(( $(date +%s) + 3600 )) run_source '
  WT_SLUG=dgn1470-write
  WT_REPO="$TDIR"
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=$RUNTIME_FALLBACK_DIRECTION"
  echo "CODEX_SLOTS=$(read_active_count codex) CLAUDE_SLOTS=$(read_active_count claude-code)"
' 2>&1)"
grep -q "RC=2" <<< "$OUT" && ok "T2 write+no-hooks refused (rc=2)" || bad "T2 rc ($OUT)"
grep -q "RUNTIME=claude-code" <<< "$OUT" \
  && ok "T2 runtime NOT switched to codex (hook contract holds for the write lane)" \
  || bad "T2 runtime switched ($OUT)"
grep -q "CODEX_SLOTS=0 CLAUDE_SLOTS=0" <<< "$OUT" && ok "T2 no slot consumed" || bad "T2 slots ($OUT)"
grep -q "DEFERRED: both lanes blocked" <<< "$OUT" \
  && ok "T2 bounded defer announced (기한 있는 대기)" || bad "T2 defer output ($OUT)"
grep -q "DEFER-STUB --label dispatch-rtd-" "$DEFER_STUB_LOG" \
  && ok "T2 re-evaluation reservation persisted via usage-defer-retry" \
  || bad "T2 reservation call ($(cat "$DEFER_STUB_LOG"))"
grep -q -- "--reset-epoch" "$DEFER_STUB_LOG" && grep -q "DOGANY_DISPATCH_DEFER_ATTEMPT=1" "$DEFER_STUB_LOG" \
  && ok "T2 replay carries reset epoch + attempt budget" || bad "T2 replay argv ($(cat "$DEFER_STUB_LOG"))"
DEC="$(latest_decision)"
if [[ -n "$DEC" && "$(dec_field "$DEC" status)" == "deferred" \
   && "$(dec_field "$DEC" reason_code)" == "both_lanes_blocked" ]]; then
  ok "T2/T6 deferred decision persisted with both lanes' reasons"
  grep -q "codex_hooks_unavailable" <<< "$(dec_field "$DEC" detail)" \
    && ok "T2/T6 codex block reason (hooks) on record" \
    || bad "T2/T6 codex reason missing ($(dec_field "$DEC" detail))"
else
  bad "T2/T6 deferred record ($DEC: $(cat "$DEC" 2>/dev/null))"
fi
PROMPT_SNAP="$(ls "$DECDIR"/rtd-*.prompt.md 2>/dev/null | head -1)"
[[ -n "$PROMPT_SNAP" ]] && grep -q "fixture task" "$PROMPT_SNAP" \
  && ok "T2 prompt snapshotted for the replay" || bad "T2 prompt snapshot missing"

# ---------------------------------------------------------------------------
echo "== T3: claude rc=2 (lookup failed) -> no switch; bounded retry only =="
reset_counts
OUT="$(GATE_STUB_RC=2 STUB_CODEX_USED=57 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=$RUNTIME_FALLBACK_DIRECTION"
' 2>&1)"
grep -q "RC=2 RUNTIME=claude-code DIR=" <<< "$OUT" \
  && ok "T3 lookup failure never switches runtime" || bad "T3 unknown-state switch ($OUT)"

reset_counts
OUT="$(GATE_STUB_RC=2 CODEX_RATELIMIT_STUB_FAIL=1 CLAUDE_USAGE_STUB_FAIL=1 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME"
' 2>&1)"
grep -q "RC=2" <<< "$OUT" && grep -q "DEFERRED: both lanes blocked" <<< "$OUT" \
  && ok "T3b claude rc=2 + codex read-fail -> bounded defer (fixed retry interval)" \
  || bad "T3b both-unreadable path ($OUT)"
# Owner 2026-09-27: a lookup failure (429) retries in about a minute, never
# on the 30-minute quota interval.
_re="$(grep -o -- '--reset-epoch [0-9]*' "$DEFER_STUB_LOG" | tail -1 | awk '{print $2}')"
_now="$(date +%s)"
if [[ "$_re" =~ ^[0-9]+$ ]] && (( _re - _now <= 90 )); then
  ok "T3c lookup failure retries within ~1 min (delta $(( _re - _now ))s)"
else
  bad "T3c lookup-failure retry not ~1 min (reset-epoch=$_re now=$_now; $(cat "$DEFER_STUB_LOG" 2>/dev/null | tail -2))"
fi
DEC="$(latest_decision)"
grep -q "not_evaluated" <<< "$(dec_field "$DEC" detail)" \
  && ok "T3b/T6 unknown state does not evaluate codex" || bad "T3b/T6 record detail ($(dec_field "$DEC" detail))"

# ---------------------------------------------------------------------------
echo "== T4: codex over-ceiling / read-failure -> reverse switch to claude, recorded =="
reset_counts
OUT="$(STUB_CODEX_USED=101 GATE_STUB_RC=0 run_source '
  RUNTIME=codex; REQUESTED_RUNTIME=codex
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=$RUNTIME_FALLBACK_DIRECTION"
  echo "CODEX_SLOTS=$(read_active_count codex) CLAUDE_SLOTS=$(read_active_count claude-code)"
' 2>&1)"
grep -q "RC=0 RUNTIME=claude-code DIR=codex->claude-code" <<< "$OUT" \
  && ok "T4 codex over-ceiling switches to claude-code (previously a dead end)" \
  || bad "T4 over-ceiling switch ($OUT)"
grep -q "CODEX_SLOTS=0 CLAUDE_SLOTS=1" <<< "$OUT" \
  && ok "T4 slot charged to claude after the switch" || bad "T4 slots ($OUT)"
DEC="$(latest_decision)"
[[ "$(dec_field "$DEC" status)" == "selected" && "$(dec_field "$DEC" fallback_direction)" == "codex->claude-code" ]] \
  && ok "T4/T6 reverse switch persisted" || bad "T4/T6 record ($(cat "$DEC" 2>/dev/null))"

reset_counts
OUT="$(CODEX_RATELIMIT_STUB_FAIL=1 GATE_STUB_RC=0 run_source '
  RUNTIME=codex; REQUESTED_RUNTIME=codex
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=$RUNTIME_FALLBACK_DIRECTION"
' 2>&1)"
grep -q "RC=0 RUNTIME=claude-code DIR=codex->claude-code" <<< "$OUT" \
  && ok "T4b codex read-failure still reaches claude (now recorded, not a side effect)" \
  || bad "T4b read-failure switch ($OUT)"
DEC="$(latest_decision)"
[[ "$(dec_field "$DEC" status)" == "selected" ]] \
  && ok "T4b/T6 read-failure switch persisted" || bad "T4b/T6 record"

# ---------------------------------------------------------------------------
echo "== T5: both lanes blocked -> reasoned closure (no unbounded wait) =="
reset_counts
OUT="$(GATE_STUB_RC=1 STUB_CODEX_USED=101 DOGANY_DISPATCH_DEFER_ATTEMPT=2 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc"
' 2>&1)"
grep -q "RC=2" <<< "$OUT" && grep -q "BLOCKED (expired): re-evaluation budget exhausted" <<< "$OUT" \
  && ok "T5 defer budget exhausted -> expired closure with reasons" || bad "T5 budget closure ($OUT)"
DEC="$(latest_decision)"
[[ "$(dec_field "$DEC" status)" == "expired" ]] \
  && ok "T5/T6 expired verdict persisted" || bad "T5/T6 expired record ($(cat "$DEC" 2>/dev/null))"
grep -vq "DEFER-STUB" "$DEFER_STUB_LOG" 2>/dev/null || [[ ! -s "$DEFER_STUB_LOG" ]] \
  && ok "T5 no reservation attempted past the budget" || bad "T5 reservation leaked"

reset_counts
OUT="$(GATE_STUB_RC=1 STUB_CODEX_USED=101 DEFER_STUB_RC=1 \
  STUB_5H_RESET=$(( $(date +%s) + 3600 )) run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc"
' 2>&1)"
grep -q "reservation could not be persisted" <<< "$OUT" \
  && ok "T5b reservation failure -> blocked, never reported as waiting" \
  || bad "T5b reservation-failure closure ($OUT)"
DEC="$(latest_decision)"
[[ "$(dec_field "$DEC" status)" == "blocked" && "$(dec_field "$DEC" reason_code)" == "defer_reservation_failed" ]] \
  && ok "T5b/T6 blocked verdict persisted" || bad "T5b/T6 record ($(cat "$DEC" 2>/dev/null))"

# ---------------------------------------------------------------------------
echo "== T7: no-regression -- default path untouched, third runtime passes, one switch max =="
reset_counts
OUT="$(GATE_STUB_RC=0 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=[$RUNTIME_FALLBACK_DIRECTION]"
  ls "$DECISION_DIR" 2>/dev/null | wc -l | tr -d " "
' 2>&1)"
grep -q "RC=0 RUNTIME=claude-code DIR=\[\]" <<< "$OUT" \
  && ok "T7 passing claude path: no switch" || bad "T7 default path ($OUT)"
grep -q "^0$" <<< "$OUT" \
  && ok "T7 passing path writes NO decision file (records are for refusals/switches)" \
  || bad "T7 spurious decision file ($OUT)"

reset_counts
OUT="$(run_source '
  RUNTIME=antigravity; REQUESTED_RUNTIME=antigravity
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=[$RUNTIME_FALLBACK_DIRECTION]"
' 2>&1)"
grep -q "RC=0 RUNTIME=antigravity DIR=\[\]" <<< "$OUT" \
  && ok "T7b unmeasured third runtime passes through untouched (3.0 boundary)" \
  || bad "T7b third runtime ($OUT)"

reset_counts
OUT="$(GATE_STUB_RC=1 CODEX_RATELIMIT_STUB_FAIL=1 CLAUDE_USAGE_STUB_FAIL=1 DEFER_STUB_RC=1 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc SWITCHES=$(grep -c "falling back to" /dev/null 2>/dev/null || true)"
  echo "DIR=$RUNTIME_FALLBACK_DIRECTION"
' 2>&1)"
N_EVAL="$(grep -c "evaluating .* fallback" <<< "$OUT" || true)"
[[ "$N_EVAL" == "1" ]] && grep -q "RC=2" <<< "$OUT" \
  && ok "T7c one switch max: exactly one alternate evaluation, then closure" \
  || bad "T7c switch count ($N_EVAL; $OUT)"

# ---------------------------------------------------------------------------
echo "== T7d: slot-cap fallback -- full primary lane hands to the alternate once =="
reset_counts
mkdir -p "$WS/.telegram_bot/dispatch"
printf '5\n' > "$WS/.telegram_bot/dispatch/active-count"   # claude cap at 10% util = 5
OUT="$(GATE_STUB_RC=0 STUB_CODEX_USED=64 STUB_HOOK_OK=1 run_source '
  rc=0; run_gates || rc=$?
  echo "RC=$rc RUNTIME=$RUNTIME DIR=$RUNTIME_FALLBACK_DIRECTION"
  echo "CODEX_SLOTS=$(read_active_count codex)"
' 2>&1)"
grep -q "RC=0 RUNTIME=codex DIR=claude-code->codex" <<< "$OUT" \
  && ok "T7d claude slots full -> codex lane takes the dispatch" || bad "T7d slot fallback ($OUT)"
grep -q "CODEX_SLOTS=1" <<< "$OUT" && ok "T7d codex slot charged" || bad "T7d codex slot ($OUT)"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
