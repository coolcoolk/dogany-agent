#!/bin/bash
# test-lane: fast
# test-dgn1768-pin-fresh.sh -- DGN-1768 r2 explicit fresh-pin request on the
# pin CLI (status-footer.py --pin-set <owner> --fresh [--wait SECS]).
#
#   F1 --pin-set without --fresh writes no rotate-request marker
#   F2 --fresh composes the section AND drops dashboard_rotate.request
#   F3 --wait returns 0 once the marker is consumed (bridge stand-in removes it)
#   F4 --wait returns nonzero on timeout and withdraws the marker
#   F5 --wait without --fresh is rejected (rc 1, nothing written)
#
# SAFETY: every call runs against a private mktemp DOGANY_BOT_DATA_DIR; no
# git, no network, nothing outside the sandbox.
set -u

FOOTER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/status-footer.py"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn1768-fresh.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
MARKER_NAME="dashboard_rotate.request"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL: $*" >&2; }

[[ -f "$FOOTER" ]] || { echo "FAIL: footer missing: $FOOTER" >&2; exit 1; }

# --- F1: plain pin-set drops no marker --------------------------------------
W1="$WORK/f1"
printf '카드' | DOGANY_BOT_DATA_DIR="$W1" python3 "$FOOTER" --pin-set brief \
  >/dev/null 2>&1
rc=$?
if [[ $rc -eq 0 && ! -e "$W1/$MARKER_NAME" ]]; then ok
else bad "F1 plain --pin-set rc=$rc or marker written"; fi

# --- F2: --fresh writes section + marker ------------------------------------
W2="$WORK/f2"
printf '오늘 일정' | DOGANY_BOT_DATA_DIR="$W2" python3 "$FOOTER" \
  --pin-set brief --fresh >/dev/null 2>&1
rc=$?
if [[ $rc -eq 0 && -f "$W2/$MARKER_NAME" ]] \
   && grep -q '오늘 일정' "$W2/dashboard.md" 2>/dev/null; then ok
else bad "F2 --fresh rc=$rc, marker/section missing"; fi
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("owner")=="brief" else 1)' \
     "$W2/$MARKER_NAME" 2>/dev/null; then ok
else bad "F2 marker does not name its owner"; fi

# --- F3: --wait returns 0 once consumed -------------------------------------
W3="$WORK/f3"
mkdir -p "$W3"
( for _ in $(seq 1 100); do
    if [[ -e "$W3/$MARKER_NAME" ]]; then rm -f "$W3/$MARKER_NAME"; exit 0; fi
    python3 -c 'import time; time.sleep(0.05)'
  done ) &
consumer=$!
start=$(date +%s)
printf '오늘 일정' | DOGANY_BOT_DATA_DIR="$W3" python3 "$FOOTER" \
  --pin-set brief --fresh --wait 10 >/dev/null 2>&1
rc=$?
elapsed=$(( $(date +%s) - start ))
wait "$consumer" 2>/dev/null
if [[ $rc -eq 0 && $elapsed -lt 10 && ! -e "$W3/$MARKER_NAME" ]]; then ok
else bad "F3 --wait on consumed marker rc=$rc elapsed=${elapsed}s"; fi

# --- F4: --wait timeout -> nonzero, marker withdrawn ------------------------
W4="$WORK/f4"
out="$(printf '오늘 일정' | DOGANY_BOT_DATA_DIR="$W4" python3 "$FOOTER" \
  --pin-set brief --fresh --wait 0.5 2>&1 >/dev/null)"
rc=$?
if [[ $rc -ne 0 && ! -e "$W4/$MARKER_NAME" ]] \
   && grep -q '오늘 일정' "$W4/dashboard.md" 2>/dev/null; then ok
else bad "F4 --wait timeout rc=$rc (want nonzero) or marker left (out=$out)"; fi

# --- F5: --wait without --fresh rejected ------------------------------------
W5="$WORK/f5"
out="$(printf 'x' | DOGANY_BOT_DATA_DIR="$W5" python3 "$FOOTER" \
  --pin-set brief --wait 1 2>&1 >/dev/null)"
rc=$?
if [[ $rc -eq 1 && "$out" == *"--wait requires --fresh"* \
      && ! -e "$W5/dashboard.md" ]]; then ok
else bad "F5 --wait without --fresh rc=$rc out=$out"; fi

echo "test-dgn1768-pin-fresh: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
