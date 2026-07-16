#!/bin/bash
# test-cron-guard-queue.sh -- self-tests for the machine-global cron queue
# in cron-guard.sh (DGN-360). Companion to test-cron-guard.sh (which covers
# the legacy alert/dedup contract).
#
# Tests (no real Telegram push; push.sh replaced by a stub; queue base dir
# redirected into an isolated temp dir via sed patch, same convention as
# test-cron-guard.sh uses for DEDUP_DIR):
#   1. concurrency cap: 6 simultaneous jobs, class heavy (default slots=2)
#      -> observed concurrency never exceeds 2, all 6 jobs complete
#   2. stale-lock reclaim: slots planted with a dead pid -> next invocation
#      reclaims and runs promptly
#   3. timeout fail-open: slots held by a LIVE pid + tiny --queue-timeout
#      -> job still runs, WARN logged, foreign slots left untouched
#   4. no-args regression: invocation without queue args behaves identically
#      to the pre-queue script (byte-diff of stdout/stderr/exit codes against
#      the git HEAD copy when it differs; behavioral asserts always)
#   5. arg validation: --slots without --queue rejected; bad class rejected
#
# Run: bash routines/tests/test-cron-guard-queue.sh
# Exit: 0 all pass, nonzero any fail. Total runtime well under 2 minutes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GUARD="$ROUTINES_DIR/cron-guard.sh"

TRUE_CMD="$(command -v true)"

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}

# ---- isolated temp workspace ----
STUB_DIR="$(mktemp -d /tmp/cron-guard-queue-test.XXXXXX)"
trap 'rm -rf "$STUB_DIR"' EXIT

PUSH_CALL_LOG="$STUB_DIR/push_calls.log"
touch "$PUSH_CALL_LOG"

STUB_PUSH="$STUB_DIR/push.sh"
cat > "$STUB_PUSH" << STUB_EOF
#!/bin/bash
_text=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --text) _text="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
_summary="\$(printf '%s' "\$_text" | tr '\n' ' ' | cut -c1-80)"
echo "PUSH: \${_summary}" >> "${PUSH_CALL_LOG}"
exit 0
STUB_EOF
chmod +x "$STUB_PUSH"

DEDUP_DIR="$STUB_DIR/dedup"
QUEUE_DIR="$STUB_DIR/queue"
mkdir -p "$DEDUP_DIR" "$QUEUE_DIR"

# ---- patched guard: redirect DEDUP_DIR, PUSH_SH, QUEUE_BASE_DIR ----
patch_guard() {
  local src="$1" dst="$2" dedup="$3" pushlog_stub="$4" qdir="$5"
  sed \
    -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$dedup\"|" \
    -e "s|PUSH_SH=\"\$SCRIPT_DIR/push.sh\"|PUSH_SH=\"$pushlog_stub\"|" \
    -e "s|QUEUE_BASE_DIR=\"\$HOME/.dogany/cron-queue\"|QUEUE_BASE_DIR=\"$qdir\"|" \
    "$src" > "$dst"
  chmod +x "$dst"
}

PATCHED_GUARD="$STUB_DIR/cron-guard-patched.sh"
patch_guard "$GUARD" "$PATCHED_GUARD" "$DEDUP_DIR" "$STUB_PUSH" "$QUEUE_DIR"

run_guard() {
  local _rc=0
  "$PATCHED_GUARD" "$@" || _rc=$?
  echo "$_rc"
}

echo ""
echo "=== cron-guard queue self-tests (DGN-360) ==="
echo ""

# =====================================================================
# test 1: concurrency cap -- 6 jobs, heavy class (default slots=2)
# =====================================================================
echo "--- test 1: 6 concurrent jobs, slots=2 -> max concurrency 2 ---"
ACTIVE_DIR="$STUB_DIR/active"
COUNTS_FILE="$STUB_DIR/counts"
DONE_DIR="$STUB_DIR/done"
mkdir -p "$ACTIVE_DIR" "$DONE_DIR"
touch "$COUNTS_FILE"

JOB="$STUB_DIR/job.sh"
cat > "$JOB" << JOB_EOF
#!/bin/bash
# Each job registers itself, samples how many jobs are active RIGHT NOW,
# holds the slot briefly, then deregisters. If the queue caps at 2 slots,
# no sample can ever exceed 2.
touch "$ACTIVE_DIR/\$\$"
c="\$(ls "$ACTIVE_DIR" | wc -l | tr -d ' ')"
echo "\$c" >> "$COUNTS_FILE"
sleep 1.5
c="\$(ls "$ACTIVE_DIR" | wc -l | tr -d ' ')"
echo "\$c" >> "$COUNTS_FILE"
rm -f "$ACTIVE_DIR/\$\$"
touch "$DONE_DIR/\$\$"
exit 0
JOB_EOF
chmod +x "$JOB"

T1_START="$(date +%s)"
i=1
while [[ $i -le 6 ]]; do
  "$PATCHED_GUARD" --label "com.test.q$i" --queue heavy -- "$JOB" 2>>"$STUB_DIR/t1.stderr" &
  i=$((i + 1))
done
wait
T1_ELAPSED=$(( $(date +%s) - T1_START ))

done_count="$(ls "$DONE_DIR" | wc -l | tr -d ' ')"
assert_eq "all 6 jobs completed" "$done_count" "6"
max_seen="$(sort -rn "$COUNTS_FILE" | head -1)"
if [[ "$max_seen" -le 2 ]]; then
  ok "max observed concurrency <= 2 (saw $max_seen)"
else
  fail "concurrency cap violated -- saw $max_seen simultaneous jobs"
fi
if [[ "$max_seen" -ge 2 ]]; then
  ok "parallelism actually happened (saw $max_seen, not serialized to 1)"
else
  fail "never saw 2 concurrent jobs -- queue over-serializes (max=$max_seen)"
fi
echo "  (samples: $(sort -n "$COUNTS_FILE" | uniq -c | tr -s ' ' | tr '\n' ';') elapsed=${T1_ELAPSED}s)"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "no failure alerts pushed" "$push_count" "0"
leftover="$(ls "$QUEUE_DIR/heavy" 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "all slots released after run" "$leftover" "0"
echo ""

# =====================================================================
# test 2: stale-lock reclaim -- dead pid in slot -> reclaimed, job runs
# =====================================================================
echo "--- test 2: stale lock (dead pid) -> reclaim and run ---"
# Manufacture a dead pid: spawn a short-lived process and reap it.
/bin/bash -c 'exit 0' &
DEAD_PID=$!
wait "$DEAD_PID" 2>/dev/null || true
if kill -0 "$DEAD_PID" 2>/dev/null; then
  fail "test setup: pid $DEAD_PID unexpectedly alive"
fi
mkdir -p "$QUEUE_DIR/heavy/slot-1" "$QUEUE_DIR/heavy/slot-2"
echo "$DEAD_PID" > "$QUEUE_DIR/heavy/slot-1/pid"
echo "$DEAD_PID" > "$QUEUE_DIR/heavy/slot-2/pid"

MARKER2="$STUB_DIR/marker-stale"
T2_START="$(date +%s)"
rc="$(run_guard --label com.test.stale --queue heavy --queue-timeout 30 -- touch "$MARKER2" 2>"$STUB_DIR/t2.stderr")"
T2_ELAPSED=$(( $(date +%s) - T2_START ))
assert_eq "exit code == 0" "$rc" "0"
[[ -f "$MARKER2" ]] && ok "job ran after reclaim" || fail "job did not run"
grep -q "reclaimed stale queue slot" "$STUB_DIR/t2.stderr" \
  && ok "reclaim logged to stderr" || fail "no reclaim log line"
if [[ "$T2_ELAPSED" -le 10 ]]; then
  ok "reclaim was prompt (${T2_ELAPSED}s, not a timeout wait)"
else
  fail "reclaim too slow (${T2_ELAPSED}s) -- looks like it waited for timeout"
fi
# the slot our job took must be released after exit
if [[ ! -d "$QUEUE_DIR/heavy/slot-1" ]]; then
  ok "acquired slot released after job"
else
  fail "acquired slot-1 still present after job exit"
fi
rm -rf "$QUEUE_DIR/heavy"
echo ""

# =====================================================================
# test 3: timeout fail-open -- live owners hold all slots
# =====================================================================
echo "--- test 3: slots exhausted by LIVE pids + tiny timeout -> fail-open ---"
mkdir -p "$QUEUE_DIR/heavy/slot-1" "$QUEUE_DIR/heavy/slot-2"
echo "$$" > "$QUEUE_DIR/heavy/slot-1/pid"   # this test script: alive
echo "$$" > "$QUEUE_DIR/heavy/slot-2/pid"

MARKER3="$STUB_DIR/marker-failopen"
T3_START="$(date +%s)"
rc="$(run_guard --label com.test.failopen --queue heavy --queue-timeout 2 -- touch "$MARKER3" 2>"$STUB_DIR/t3.stderr")"
T3_ELAPSED=$(( $(date +%s) - T3_START ))
assert_eq "exit code == 0" "$rc" "0"
[[ -f "$MARKER3" ]] && ok "job ran despite exhausted slots (fail-open)" || fail "job did not run"
grep -q "WARN: queue timeout" "$STUB_DIR/t3.stderr" \
  && ok "WARN logged" || fail "WARN line missing"
grep -q "fail-open" "$STUB_DIR/t3.stderr" \
  && ok "fail-open stated in WARN" || fail "fail-open wording missing"
if [[ "$T3_ELAPSED" -ge 2 ]]; then
  ok "actually waited for the timeout (${T3_ELAPSED}s >= 2s)"
else
  fail "did not wait (${T3_ELAPSED}s < 2s timeout)"
fi
# Foreign live slots must be untouched (fail-open run holds NO slot)
own1="$(cat "$QUEUE_DIR/heavy/slot-1/pid" 2>/dev/null)"
own2="$(cat "$QUEUE_DIR/heavy/slot-2/pid" 2>/dev/null)"
if [[ "$own1" == "$$" && "$own2" == "$$" ]]; then
  ok "foreign live slots left intact"
else
  fail "fail-open run disturbed foreign slots (slot1=$own1 slot2=$own2)"
fi
rm -rf "$QUEUE_DIR/heavy"
echo ""

# =====================================================================
# test 4: no-args regression -- identical to pre-queue behavior
# =====================================================================
echo "--- test 4: no queue args -> legacy behavior (regression) ---"
# 4a. behavioral asserts on the new script
: > "$PUSH_CALL_LOG"
rm -rf "$DEDUP_DIR"; mkdir -p "$DEDUP_DIR"
rc="$(run_guard --label com.test.legacy -- "$TRUE_CMD")"
assert_eq "success: exit 0" "$rc" "0"
rc="$(run_guard --label com.test.legacy -- /bin/bash -c 'exit 7')"
assert_eq "failure: exit code 7 preserved" "$rc" "7"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "failure alert pushed once" "$push_count" "1"
rc="$(run_guard --label com.test.legacy -- /bin/bash -c 'exit 7' 2>/dev/null)"
assert_eq "re-failure: exit code preserved under dedup" "$rc" "7"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "dedup suppressed second alert" "$push_count" "1"
qtouch="$(ls "$QUEUE_DIR" 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "queue dir untouched in no-args mode" "$qtouch" "0"

# 4b. byte-diff against the pre-queue git HEAD copy (when it differs)
OLD_SRC="$STUB_DIR/cron-guard-head.sh"
if git -C "$ROUTINES_DIR" show "HEAD:./cron-guard.sh" > "$OLD_SRC" 2>/dev/null \
   && ! diff -q "$OLD_SRC" "$GUARD" >/dev/null 2>&1; then
  OLD_DEDUP="$STUB_DIR/dedup-old"
  OLD_PUSH_LOG="$STUB_DIR/push_calls_old.log"
  mkdir -p "$OLD_DEDUP"; touch "$OLD_PUSH_LOG"
  OLD_STUB_PUSH="$STUB_DIR/push-old.sh"
  sed "s|${PUSH_CALL_LOG}|${OLD_PUSH_LOG}|" "$STUB_PUSH" > "$OLD_STUB_PUSH"
  chmod +x "$OLD_STUB_PUSH"
  OLD_GUARD="$STUB_DIR/cron-guard-old-patched.sh"
  patch_guard "$OLD_SRC" "$OLD_GUARD" "$OLD_DEDUP" "$OLD_STUB_PUSH" "$QUEUE_DIR"

  NEW_DEDUP="$STUB_DIR/dedup-new"
  NEW_PUSH_LOG="$STUB_DIR/push_calls_new.log"
  mkdir -p "$NEW_DEDUP"; touch "$NEW_PUSH_LOG"
  NEW_STUB_PUSH="$STUB_DIR/push-new.sh"
  sed "s|${PUSH_CALL_LOG}|${NEW_PUSH_LOG}|" "$STUB_PUSH" > "$NEW_STUB_PUSH"
  chmod +x "$NEW_STUB_PUSH"
  NEW_GUARD="$STUB_DIR/cron-guard-new-patched.sh"
  patch_guard "$GUARD" "$NEW_GUARD" "$NEW_DEDUP" "$NEW_STUB_PUSH" "$QUEUE_DIR"

  DUMMY_LOG="$STUB_DIR/dummy.stdout.log"
  printf 'line1\nline2\nline3\n' > "$DUMMY_LOG"

  compare_run() {
    local desc="$1"; shift
    local orc=0 nrc=0
    "$OLD_GUARD" "$@" > "$STUB_DIR/old.out" 2> "$STUB_DIR/old.err" || orc=$?
    "$NEW_GUARD" "$@" > "$STUB_DIR/new.out" 2> "$STUB_DIR/new.err" || nrc=$?
    if [[ "$orc" == "$nrc" ]] \
       && diff -q "$STUB_DIR/old.out" "$STUB_DIR/new.out" >/dev/null \
       && diff -q "$STUB_DIR/old.err" "$STUB_DIR/new.err" >/dev/null; then
      ok "old==new: $desc (exit $orc, stdout+stderr identical)"
    else
      fail "old!=new: $desc (old rc=$orc new rc=$nrc)"
      diff "$STUB_DIR/old.err" "$STUB_DIR/new.err" | head -5 | sed 's/^/    /'
    fi
  }

  compare_run "success job"        --label com.test.diff --log "$DUMMY_LOG" -- "$TRUE_CMD"
  compare_run "failure job exit 9" --label com.test.diff --log "$DUMMY_LOG" -- /bin/bash -c 'exit 9'
  compare_run "dedup re-failure"   --label com.test.diff --log "$DUMMY_LOG" -- /bin/bash -c 'exit 9'
  compare_run "friendly-name failure" --label com.test.diff2 --friendly-name "My Job" --log "$DUMMY_LOG" -- /bin/bash -c 'exit 4'
  compare_run "missing --label"    -- "$TRUE_CMD"
  compare_run "unknown arg"        --label com.test.diff --bogus x -- "$TRUE_CMD"
  if diff -q "$OLD_PUSH_LOG" "$NEW_PUSH_LOG" >/dev/null; then
    ok "old==new: push payloads byte-identical"
  else
    fail "old!=new: push payloads differ"
  fi
else
  echo "  SKIP: git HEAD copy identical or unavailable -- byte-diff not applicable"
fi
echo ""

# =====================================================================
# test 5: arg validation
# =====================================================================
echo "--- test 5: arg validation ---"
rc="$(run_guard --label com.test.val --slots 2 -- "$TRUE_CMD" 2>/dev/null)"
assert_eq "--slots without --queue rejected (exit 1)" "$rc" "1"
rc="$(run_guard --label com.test.val --queue 'bad/class' -- "$TRUE_CMD" 2>/dev/null)"
assert_eq "class with slash rejected (exit 1)" "$rc" "1"
rc="$(run_guard --label com.test.val --queue heavy --slots 0 -- "$TRUE_CMD" 2>/dev/null)"
assert_eq "--slots 0 rejected (exit 1)" "$rc" "1"
rc="$(run_guard --label com.test.val --queue heavy --queue-timeout x -- "$TRUE_CMD" 2>/dev/null)"
assert_eq "non-numeric timeout rejected (exit 1)" "$rc" "1"
echo ""

# ---- summary ----
echo "==========================="
echo "Results: $PASS passed, $FAIL failed"
echo "==========================="
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
