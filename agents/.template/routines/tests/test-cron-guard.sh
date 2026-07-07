#!/bin/bash
# test-cron-guard.sh -- unit tests for cron-guard.sh
#
# Tests (no real Telegram push; push.sh replaced by a stub):
#   1. success job (exit 0)  -> no alert
#   2. failure job (exit 1)  -> alert fired once, dedup file created
#   3. same failure, same day -> dedup suppresses second alert
#   4. success after prior failure -> still silent (exit 0)
#   5. different label same day -> independent dedup fires
#   6. alert text sanity (contains label + exit keyword)
#   7. no --log arg -> default log path derivation (no crash)
#
# Run: bash routines/tests/test-cron-guard.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GUARD="$ROUTINES_DIR/cron-guard.sh"

# portable true/false (macOS: /usr/bin, not /bin)
TRUE_CMD="$(command -v true)"
FALSE_CMD="$(command -v false)"

# ---- test harness ----
PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}

# run guard, capture exit code safely (no || true which swallows the code)
run_guard() {
  local _rc=0
  "$PATCHED_GUARD" "$@" || _rc=$?
  echo "$_rc"
}

# ---- isolated temp workspace ----
STUB_DIR="$(mktemp -d /tmp/cron-guard-test.XXXXXX)"
trap 'rm -rf "$STUB_DIR"' EXIT

PUSH_CALL_LOG="$STUB_DIR/push_calls.log"
touch "$PUSH_CALL_LOG"

# ---- stub push.sh
# Writes one line per invocation: first --text argument (truncated) + arg count.
# Avoids multiline append by summarising rather than echoing $*.
STUB_PUSH="$STUB_DIR/push.sh"
cat > "$STUB_PUSH" << STUB_EOF
#!/bin/bash
# stub push.sh -- records calls without real Telegram send
# Extract the --text value (first arg after --text flag)
_text=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --text) _text="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
# Write a single summary line (first 80 chars of text, no newlines)
_summary="\$(printf '%s' "\$_text" | tr '\n' ' ' | cut -c1-80)"
echo "PUSH: \${_summary}" >> "${PUSH_CALL_LOG}"
exit 0
STUB_EOF
chmod +x "$STUB_PUSH"

# ---- isolated dedup dir ----
DEDUP_DIR="$STUB_DIR/dedup"
mkdir -p "$DEDUP_DIR"

# ---- dummy log file for log-tail test ----
DUMMY_LOG="$STUB_DIR/dummy.stdout.log"
printf 'line1: some output\nline2: more output\nline3: final line\n' > "$DUMMY_LOG"

# ---- patched guard: replace DEDUP_DIR and PUSH_SH in a copy ----
PATCHED_GUARD="$STUB_DIR/cron-guard-patched.sh"
sed \
  -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$DEDUP_DIR\"|" \
  -e "s|PUSH_SH=\"\$SCRIPT_DIR/push.sh\"|PUSH_SH=\"$STUB_PUSH\"|" \
  "$GUARD" > "$PATCHED_GUARD"
chmod +x "$PATCHED_GUARD"

LABEL="com.test.cron-guard-test"
TODAY="$(date +%Y%m%d)"

echo ""
echo "=== cron-guard self-tests ==="
echo ""

# ---- test 1: success job -> no alert ----
echo "--- test 1: success job (exit 0) -> no alert ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- "$TRUE_CMD")"
assert_eq "exit code == 0" "$rc" "0"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push NOT called for success" "$push_count" "0"
echo ""

# ---- test 2: failure job -> alert fired once ----
echo "--- test 2: failure job (exit 1) -> alert fired ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- "$FALSE_CMD")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called exactly once" "$push_count" "1"
dedup_file="$DEDUP_DIR/${LABEL}.${TODAY}"
[[ -f "$dedup_file" ]] && ok "dedup file created" || fail "dedup file missing ($dedup_file)"
echo ""

# ---- test 3: same label, same day re-failure -> dedup suppresses ----
echo "--- test 3: same label re-failure (same day) -> dedup suppresses ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- /bin/bash -c "exit 2")"
assert_eq "exit code == 2 (preserved even on dedup)" "$rc" "2"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push still only 1 call (dedup suppressed)" "$push_count" "1"
echo ""

# ---- test 4: success after prior failure -> no push ----
echo "--- test 4: success after prior failure -> no push ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- "$TRUE_CMD")"
assert_eq "exit code == 0" "$rc" "0"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push count unchanged at 1" "$push_count" "1"
echo ""

# ---- test 5: different label same day -> independent dedup ----
echo "--- test 5: different label same day -> fires independently ---"
LABEL2="com.test.cron-guard-other"
rc="$(run_guard --label "$LABEL2" --log "$DUMMY_LOG" -- /bin/bash -c "exit 5")"
assert_eq "exit code == 5 (different label, preserved)" "$rc" "5"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called 2 total (second label fires)" "$push_count" "2"
dedup_file2="$DEDUP_DIR/${LABEL2}.${TODAY}"
[[ -f "$dedup_file2" ]] && ok "dedup file created for second label" || fail "dedup file missing for second label"
echo ""

# ---- test 6: alert text sanity check ----
echo "--- test 6: alert text contains label + exit keyword ---"
last_call="$(tail -1 "$PUSH_CALL_LOG")"
echo "  last push call: $last_call"
[[ "$last_call" == *"$LABEL2"* ]] && ok "alert contains label" || fail "alert missing label"
[[ "$last_call" == *"exit"* ]] && ok "alert contains exit keyword" || fail "alert missing exit keyword"
echo ""

# ---- test 7: no --log arg -> default log path derivation (no crash) ----
echo "--- test 7: no --log arg (default path fallback) -> no crash ---"
LABEL3="com.test.cron-guard-nolog"
rc="$(run_guard --label "$LABEL3" -- /bin/bash -c "exit 3")"
assert_eq "exit code == 3 (no --log, preserved)" "$rc" "3"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called 3 total" "$push_count" "3"
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
