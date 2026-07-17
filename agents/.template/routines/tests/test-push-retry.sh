#!/bin/bash
# test-push-retry.sh -- unit tests for push.sh generation retry logic (DGN-395)
#
# Tests (no real Telegram send; claude and curl replaced by stubs):
#   1. claude always empty -> 3 total attempts, exit 1, stderr reason line
#   2. claude empty on first attempt, succeeds on second -> exit 0 (transient survives)
#
# Run: bash routines/tests/test-push-retry.sh
# Exit: 0 all pass, nonzero any fail.
#
# Note: test 1 incurs ~10s backoff sleep (2 x 5s between 3 attempts).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PUSH_SH="$ROUTINES_DIR/push.sh"

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}

# ---- isolated temp workspace ----
STUB_DIR="$(mktemp -d /tmp/push-retry-test.XXXXXX)"
trap 'rm -rf "$STUB_DIR"' EXIT

# ---- fake .env (non-placeholder token; no real Telegram send needed) ----
FAKE_ENV="$STUB_DIR/fake.env"
cat > "$FAKE_ENV" << 'ENV_EOF'
TELEGRAM_BOT_TOKEN=fake:token123
ALLOWED_USER_IDS=12345
ENV_EOF

# ---- stub curl: always returns HTTP 200 with empty ok body ----
STUB_CURL="$STUB_DIR/curl"
cat > "$STUB_CURL" << 'CURL_EOF'
#!/bin/bash
_ofile=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then _ofile="$2"; shift 2; else shift; fi
done
[[ -n "$_ofile" ]] && echo '{"ok":true}' > "$_ofile"
echo "200"
CURL_EOF
chmod +x "$STUB_CURL"

echo ""
echo "=== push.sh retry self-tests (DGN-395) ==="
echo ""

# =====================================================================
# test 1: claude always returns empty -> 3 attempts, exit 1, stderr reason
# =====================================================================
echo "--- test 1: claude always empty -> 3 attempts, exit 1, stderr reason ---"
echo "(~10s backoff sleep expected)"
ATTEMPT_LOG1="$STUB_DIR/attempts1.log"
touch "$ATTEMPT_LOG1"

STUB_CLAUDE="$STUB_DIR/claude"
cat > "$STUB_CLAUDE" << CLAUDE_EOF
#!/bin/bash
echo "attempt" >> "${ATTEMPT_LOG1}"
exit 0
CLAUDE_EOF
chmod +x "$STUB_CLAUDE"

STDERR1="$STUB_DIR/t1.stderr"
rc1=0
PATH="$STUB_DIR:$PATH" "$PUSH_SH" --env "$FAKE_ENV" --prompt "test" 2>"$STDERR1" || rc1=$?

attempt_count1="$(wc -l < "$ATTEMPT_LOG1" | tr -d ' ')"
assert_eq "3 total claude attempts (1 initial + 2 retries)" "$attempt_count1" "3"
assert_eq "exit code 1 after all retries exhausted" "$rc1" "1"
grep -q "claude returned empty after" "$STDERR1" \
  && ok "stderr reason line present" \
  || fail "stderr reason line missing (got: $(cat "$STDERR1"))"
grep -q "3 attempts" "$STDERR1" \
  && ok "stderr reason cites attempt count (3)" \
  || fail "attempt count not in stderr reason (got: $(cat "$STDERR1"))"
echo ""

# =====================================================================
# test 2: claude empty on first attempt, succeeds on second -> exit 0
# =====================================================================
echo "--- test 2: claude empty once then success -> exit 0 (transient survived) ---"
echo "(~5s backoff sleep expected)"
ATTEMPT_LOG2="$STUB_DIR/attempts2.log"
touch "$ATTEMPT_LOG2"

cat > "$STUB_CLAUDE" << CLAUDE_EOF
#!/bin/bash
echo "attempt" >> "${ATTEMPT_LOG2}"
cnt=\$(wc -l < "${ATTEMPT_LOG2}" | tr -d ' ')
if [[ "\$cnt" -ge 2 ]]; then
  echo "hello from claude"
fi
exit 0
CLAUDE_EOF
chmod +x "$STUB_CLAUDE"

STDERR2="$STUB_DIR/t2.stderr"
rc2=0
PATH="$STUB_DIR:$PATH" "$PUSH_SH" --env "$FAKE_ENV" --prompt "test" 2>"$STDERR2" || rc2=$?

attempt_count2="$(wc -l < "$ATTEMPT_LOG2" | tr -d ' ')"
assert_eq "2 claude attempts (empty then success)" "$attempt_count2" "2"
assert_eq "exit code 0 (transient empty survived)" "$rc2" "0"
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
