#!/bin/bash
# test-lane: fast
# test-dgn1873-classify-retry-routing.sh -- classify-inbox-check.sh exit
# contract through the real cron-guard.sh (DGN-1873, owner directive
# 2026-10-06: a failure that recovers by itself must not reach the owner).
#
#   R1. first classify failure -> exit 78 + retry marker -> operator-only
#       notice, no button; no 7-day marker; retry pending recorded.
#   R2. the retry fails too -> exit 1 -> the normal failure alarm.
#   R3. a good run clears the pending retry.
#   R4. classify ok but reindex deferred (memory.py rc 3) -> 7-day marker
#       written + reindex pending + exit 78 (operator only).
#   R5. next run retries the reindex alone (index --wait-lock), clears both
#       pending states, then honours the 7-day marker (no new classify).
#   R6. the pending reindex fails again -> exit 1 -> failure alarm.
#   R7. a classify retry that succeeds with a deferred reindex is a NEW
#       retry (78), not the old retry failing (1).
#
# memory.py is a stub; push.sh is a stub. Run:
#   bash routines/tests/test-dgn1873-classify-retry-routing.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_eq() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 -- got='$2' want='$3'"; fi
}

T="$(mktemp -d /tmp/dgn1873-classify.XXXXXX)"
trap 'rm -rf "$T"' EXIT

I="$T/inst"
H="$T/home"
mkdir -p "$I/routines/lib" "$I/memory-engine" "$I/identity" "$H"
cp "$ROUTINES_DIR/classify-inbox-check.sh" "$I/routines/"
cp "$ROUTINES_DIR/lib/agent_name.py" "$ROUTINES_DIR/lib/scheduled_job_names.py" "$I/routines/lib/"
sed -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$T/dedup\"|" \
  "$ROUTINES_DIR/cron-guard.sh" > "$I/routines/cron-guard.sh"
chmod +x "$I/routines/"*.sh
printf '# AGENT\n- Name: **김도가**\n' > "$I/identity/hot.custom.agent.md"

# push.sh stub: one line per call "<audience> fallback=<0|1> button=<0|1>",
# full text of the latest call in push_full.
cat > "$I/routines/push.sh" <<EOF
#!/bin/bash
aud=owner; nofb=0; btn=0; text=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --text) text="\$2"; shift 2 ;;
    --audience) aud="\$2"; shift 2 ;;
    --button) btn=1; shift 2 ;;
    --no-owner-fallback) nofb=1; shift ;;
    *) shift ;;
  esac
done
echo "\$aud nofallback=\$nofb button=\$btn" >> "$T/push_calls"
printf '%s' "\$text" > "$T/push_full"
exit 0
EOF
chmod +x "$I/routines/push.sh"

# memory.py stub: rc per subcommand from control files; every call logged.
cat > "$I/memory-engine/memory.py" <<'EOF'
import os, sys
d = os.path.dirname(os.path.abspath(__file__))
def rc(name):
    try:
        return int(open(os.path.join(d, name)).read().strip())
    except OSError:
        return 0
with open(os.path.join(d, "calls"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
cmd = sys.argv[1]
if cmd == "inbox-count":
    print(3)
    sys.exit(0)
if cmd == "classify-inbox":
    sys.exit(rc("classify_rc"))
if cmd == "index":
    sys.exit(rc("index_rc"))
sys.exit(1)
EOF

MARKER="$I/.classify_inbox_last"
RETRY_PENDING="$I/.classify_retry_pending"
REINDEX_PENDING="$I/.classify_reindex_pending"

# run_check <label-suffix> -> rc; a new day each run (the per-day dedup
# keys roll over between the dawn step and its retry in real life only when
# the labels differ, which they do: dmq-classify vs classify-inbox-0500).
run_check() {
  local _rc=0
  rm -rf "$T/dedup"
  : > "$T/push_calls"
  : > "$I/memory-engine/calls"
  HOME="$H" "$I/routines/cron-guard.sh" --label "com.telegram-skill-bot.ag.$1" \
    -- /bin/bash "$I/routines/classify-inbox-check.sh" > "$T/out" 2>&1 || _rc=$?
  echo "$_rc"
}
set_rc() { echo "$2" > "$I/memory-engine/$1"; }

echo "R1: first classify failure -> retry scheduled, operator only"
set_rc classify_rc 1
rc="$(run_check dmq-classify)"
assert_eq "R1 exit 78" "$rc" "78"
assert_eq "R1 one operator-only push, no button" "$(cat "$T/push_calls")" "operator nofallback=1 button=0"
[[ "$(cat "$T/push_full")" == *"> 단계: classify"* ]] && ok "R1 notice names the classify step" \
  || fail "R1 notice: $(cat "$T/push_full")"
[[ -f "$RETRY_PENDING" && ! -f "$MARKER" ]] && ok "R1 retry pending, no 7-day marker" \
  || fail "R1 state: pending=$([[ -f $RETRY_PENDING ]] && echo y) marker=$([[ -f $MARKER ]] && echo y)"

echo "R2: the retry fails too -> failure alarm"
rc="$(run_check classify-inbox-0500)"
assert_eq "R2 exit 1" "$rc" "1"
assert_eq "R2 failure alarm (owner fallback allowed)" "$(cat "$T/push_calls")" "operator nofallback=0 button=1"
[[ "$(cat "$T/push_full")" == *"기억 분류 작업이 방금 실패했어요."* ]] && ok "R2 approved failure copy" \
  || fail "R2 copy: $(cat "$T/push_full")"

echo "R3: a good run clears the pending retry"
set_rc classify_rc 0
rc="$(run_check classify-inbox-0500)"
assert_eq "R3 exit 0" "$rc" "0"
assert_eq "R3 nothing pushed" "$(wc -c < "$T/push_calls" | tr -d ' ')" "0"
[[ ! -f "$RETRY_PENDING" && -f "$MARKER" ]] && ok "R3 pending cleared, marker written" \
  || fail "R3 state"

echo "R4: classified, reindex deferred (rc 3) -> marker kept, reindex retried later"
rm -f "$MARKER"
set_rc classify_rc 3
rc="$(run_check dmq-classify)"
assert_eq "R4 exit 78" "$rc" "78"
assert_eq "R4 operator-only push" "$(cat "$T/push_calls")" "operator nofallback=1 button=0"
[[ "$(cat "$T/push_full")" == *"> 단계: reindex"* ]] && ok "R4 notice names the reindex step" \
  || fail "R4 notice: $(cat "$T/push_full")"
[[ -f "$MARKER" && -f "$REINDEX_PENDING" && -f "$RETRY_PENDING" ]] \
  && ok "R4 7-day marker + reindex pending + retry pending" || fail "R4 state"

echo "R5: next run retries the reindex alone"
set_rc index_rc 0
rc="$(run_check classify-inbox-0500)"
assert_eq "R5 exit 0" "$rc" "0"
assert_eq "R5 memory.py calls: index --wait-lock only" "$(cat "$I/memory-engine/calls")" "index --wait-lock"
[[ ! -f "$REINDEX_PENDING" && ! -f "$RETRY_PENDING" ]] && ok "R5 both pending states cleared" \
  || fail "R5 state"
assert_eq "R5 nothing pushed" "$(wc -c < "$T/push_calls" | tr -d ' ')" "0"

echo "R6: the pending reindex fails again -> failure alarm"
rm -f "$MARKER"
set_rc classify_rc 3
rc="$(run_check dmq-classify)"
assert_eq "R6 first: exit 78" "$rc" "78"
set_rc index_rc 1
rc="$(run_check classify-inbox-0500)"
assert_eq "R6 retry: exit 1" "$rc" "1"
assert_eq "R6 failure alarm" "$(cat "$T/push_calls")" "operator nofallback=0 button=1"
[[ -f "$REINDEX_PENDING" ]] && ok "R6 reindex stays pending" || fail "R6 pending lost"

echo "R7: a classify retry that classifies but defers the reindex is a new retry"
rm -f "$MARKER" "$REINDEX_PENDING" "$RETRY_PENDING"
set_rc index_rc 0
set_rc classify_rc 1
rc="$(run_check dmq-classify)"
assert_eq "R7 classify fails: exit 78" "$rc" "78"
set_rc classify_rc 3
rc="$(run_check classify-inbox-0500)"
assert_eq "R7 retry classifies, reindex deferred: exit 78 (not 1)" "$rc" "78"
assert_eq "R7 operator only" "$(cat "$T/push_calls")" "operator nofallback=1 button=0"

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
