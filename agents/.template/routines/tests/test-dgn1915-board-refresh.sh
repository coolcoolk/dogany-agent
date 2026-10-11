#!/bin/bash
# test-lane: fast
# test-dgn1915-board-refresh.sh -- DGN-1915 out-of-turn workbench refresh.
# The pinned board used to be recomposed only at the owner session's Stop, so
# a dispatch started/ended between owner turns never reached the pin.
#
#   R1 the owner Stop records its transcript (workbench-owner.json)
#   R2 --board-refresh with a ledger running row puts it on the board
#      (no owner turn in between)
#   R3 a terminal ledger write + --board-refresh clears the row again
#   R4 no recorded owner -> refresh exits 0 and writes nothing at all
#   R5 recorded transcript is no longer the live owner -> no write
#   R6 debounce: a burst of concurrent calls -> one worker refreshes, the
#      rest return at once; the board still lands
#   R7 --board-refresh with an argument is a usage error (rc 1)
#   R8 lib/ledger-update.sh fires the refresh on a DEFAULT-ledger write,
#      not on an overridden --ledger, not under DOGANY_BOARD_REFRESH=0
#
# SAFETY: every call runs against private mktemp dirs (DOGANY_BOT_DATA_DIR,
# DOGANY_SESSIONS_FILE, DOGANY_LEDGER_FILE, HOME); no git, no network.
set -u

ROUTINES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FOOTER="$ROUTINES/status-footer.py"
LEDGER_UPDATE="$ROUTINES/lib/ledger-update.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn1915-refresh.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL: $*" >&2; }

[[ -f "$FOOTER" ]] || { echo "FAIL: footer missing: $FOOTER" >&2; exit 1; }

SID="11111111-2222-3333-4444-555555555555"
TRANSCRIPT="$WORK/home/.claude/projects/-enc/$SID.jsonl"
mkdir -p "$(dirname "$TRANSCRIPT")" "$WORK/data"
: > "$TRANSCRIPT"
printf '{"chat": {"session_id": "%s"}}\n' "$SID" > "$WORK/sessions.json"
LEDGER="$WORK/ledger.md"
seed_ledger() {
  printf '%s\n' \
    "| branch | item | state | attempts | backoff_until | last_ts | flags | note |" \
    "|---|---|---|---|---|---|---|---|" > "$LEDGER"
}
seed_ledger
: > "$WORK/decisions.md"

export HOME="$WORK/home"
export DOGANY_BOT_DATA_DIR="$WORK/data"
export DOGANY_SESSIONS_FILE="$WORK/sessions.json"
export DOGANY_LEDGER_FILE="$LEDGER"
export DOGANY_DECISIONS_FILE="$WORK/decisions.md"
export DOGANY_DISPATCH_DIR="$WORK/dispatch"
export DOGANY_UNPARK_FILE="$WORK/unpark.md"
export DOGANY_LEDGER_REVIEW_CACHE="$WORK/review-cache.json"
export DOGANY_SUBAGENT_STOPS_DIR="$WORK/stops"
export DOGANY_BOARD_PLUGIN=none
export DOGANY_BOARD_REFRESH_DEBOUNCE=0
# The test's own ledger writes must never fire a real refresh.
export DOGANY_BOARD_REFRESH=0

DASH="$WORK/data/dashboard.md"

# --- R1: owner Stop records its transcript ---------------------------------
printf '{"transcript_path": "%s"}' "$TRANSCRIPT" \
  | python3 "$FOOTER" >/dev/null 2>&1
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("transcript_path")==sys.argv[2] else 1)' \
     "$WORK/data/workbench-owner.json" "$TRANSCRIPT" 2>/dev/null; then ok
else bad "R1 owner Stop did not record workbench-owner.json"; fi

# --- R2: a running row lands without an owner turn -------------------------
"$LEDGER_UPDATE" dsp/dgn1915-a running "model=sonnet" --item "dgn1915 alpha" \
  --ledger "$LEDGER" >/dev/null 2>&1 || bad "R2 ledger seed write failed"
python3 "$FOOTER" --board-refresh >/dev/null 2>&1
rc=$?
if [[ $rc -eq 0 ]] && grep -q 'dgn1915 alpha' "$DASH" 2>/dev/null; then ok
else bad "R2 refresh rc=$rc, running row missing from board"; fi

# --- R3: terminal write + refresh clears the row ----------------------------
"$LEDGER_UPDATE" dsp/dgn1915-a pending-review "done" \
  --ledger "$LEDGER" >/dev/null 2>&1 || bad "R3 ledger terminal write failed"
python3 "$FOOTER" --board-refresh >/dev/null 2>&1
if ! grep -q 'dgn1915 alpha' "$DASH" 2>/dev/null; then ok
else bad "R3 terminal row still on the board after refresh"; fi

# --- R4: no recorded owner -> no write --------------------------------------
D4="$WORK/data4"
mkdir -p "$D4"
"$LEDGER_UPDATE" dsp/dgn1915-b running "x" --item "dgn1915 beta" \
  --ledger "$LEDGER" >/dev/null 2>&1
DOGANY_BOT_DATA_DIR="$D4" python3 "$FOOTER" --board-refresh >/dev/null 2>&1
rc=$?
if [[ $rc -eq 0 && -z "$(ls -A "$D4")" ]]; then ok
else bad "R4 refresh without recorded owner rc=$rc or wrote: $(ls -A "$D4")"; fi

# --- R5: recorded transcript no longer the owner -> no write ----------------
D5="$WORK/data5"
mkdir -p "$D5"
printf '{"transcript_path": "%s"}' "$WORK/home/.claude/projects/-enc/other.jsonl" \
  > "$D5/workbench-owner.json"
DOGANY_BOT_DATA_DIR="$D5" python3 "$FOOTER" --board-refresh >/dev/null 2>&1
rc=$?
if [[ $rc -eq 0 && "$(ls -A "$D5")" == "workbench-owner.json" ]]; then ok
else bad "R5 refresh for a non-owner transcript rc=$rc or wrote: $(ls -A "$D5")"; fi

# --- R6: debounce -- burst of 5 calls, one worker ----------------------------
rm -f "$DASH"
pids=()
start=$(date +%s.%N)
for _ in 1 2 3 4 5; do
  ( DOGANY_BOARD_REFRESH_DEBOUNCE=1.5 python3 "$FOOTER" --board-refresh \
      >/dev/null 2>&1
    python3 -c 'import sys,time; print("%.2f" % (time.time()-float(sys.argv[1])))' \
      "$start" >> "$WORK/r6.times" ) &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done
slow=$(awk '$1 >= 1.4' "$WORK/r6.times" | wc -l | tr -d ' ')
fast=$(awk '$1 < 1.4' "$WORK/r6.times" | wc -l | tr -d ' ')
if [[ "$fast" -ge 3 && "$slow" -ge 1 ]]; then ok
else bad "R6 debounce: fast=$fast slow=$slow (times: $(tr '\n' ' ' < "$WORK/r6.times"))"; fi
if grep -q 'dgn1915 beta' "$DASH" 2>/dev/null; then ok
else bad "R6 burst refresh did not land the board"; fi

# --- R7: usage error ---------------------------------------------------------
python3 "$FOOTER" --board-refresh extra >/dev/null 2>&1
rc=$?
if [[ $rc -eq 1 ]]; then ok; else bad "R7 extra argument rc=$rc (want 1)"; fi

# --- R8: ledger-update.sh trigger rule (stub footer in a copied tree) --------
T="$WORK/tree"
mkdir -p "$T/routines/lib" "$T/product"
cp "$LEDGER_UPDATE" "$T/routines/lib/ledger-update.sh"
cat > "$T/routines/status-footer.py" <<'PY'
import os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "refresh.calls"), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
PY
LEDGER="$T/product/auto-loop-ledger.md"
seed_ledger
CALLS="$T/routines/refresh.calls"
wait_calls() {  # wait_calls <n>: up to ~3s for the detached stub
  local i
  for i in $(seq 1 60); do
    [[ -f "$CALLS" && $(wc -l < "$CALLS") -ge $1 ]] && return 0
    python3 -c 'import time; time.sleep(0.05)'
  done
  return 1
}
DOGANY_BOARD_REFRESH=1 "$T/routines/lib/ledger-update.sh" dsp/x running "n" \
  >/dev/null 2>&1
if wait_calls 1 && grep -qx -- '--board-refresh' "$CALLS"; then ok
else bad "R8 default-ledger write did not fire --board-refresh"; fi
DOGANY_BOARD_REFRESH=1 "$T/routines/lib/ledger-update.sh" dsp/y running "n" \
  --ledger "$WORK/ledger.md" >/dev/null 2>&1
DOGANY_BOARD_REFRESH=0 "$T/routines/lib/ledger-update.sh" dsp/x failed "n" \
  >/dev/null 2>&1
python3 -c 'import time; time.sleep(0.5)'
n=$(wc -l < "$CALLS" | tr -d ' ')
if [[ "$n" -eq 1 ]]; then ok
else bad "R8 override/opt-out still fired a refresh (calls=$n)"; fi
if grep -q '| dsp/x | - | failed |' "$LEDGER"; then ok
else bad "R8 ledger write itself did not land"; fi

echo "test-dgn1915-board-refresh: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
