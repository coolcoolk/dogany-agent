#!/bin/bash
# test-push-guard-dgn1122.sh -- push.sh sender-side test-context guard.
#
# Verification order is CONTRACTUAL (pgrep -af precedent): first PROVE the
# firing detector (curl stub argv capture) can see a real launch, THEN prove
# the guard blocks it. A "blocked" report from a detector that never saw a
# positive is meaningless.
#
#   1. positive self-verification: guard bypassed (PUSH_GUARD_OVERRIDE)
#      -> the send FIRES and the stub captures it (+ override receipt logged)
#   2. guard on, ancestry layer: this harness lives under routines/tests/
#      -> LOUD BLOCKED, exit 3, ZERO curl calls
#   3. guard on, env-marker layer: PYTEST_CURRENT_TEST set
#      -> LOUD BLOCKED naming the marker, exit 3, ZERO curl calls
#   4. false-positive zero: a routine-shaped caller (non-tests path, clean
#      env, ancestry reparented to init/launchd via double-fork)
#      -> push fires normally, rc 0, no guard output at all
#
# No real Telegram send anywhere: curl is stubbed and its argv captured.
#
# Run: bash routines/tests/test-push-guard-dgn1122.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PUSH_SH="$ROUTINES_DIR/push.sh"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_contains() {
  local desc="$1" hay="$2" needle="$3"
  if [[ "$hay" == *"$needle"* ]]; then ok "$desc"; else fail "$desc -- missing '$needle' in: $hay"; fi
}
assert_not_contains() {
  local desc="$1" hay="$2" needle="$3"
  if [[ "$hay" != *"$needle"* ]]; then ok "$desc"; else fail "$desc -- unexpected '$needle' in: $hay"; fi
}
assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}

TMP="$(mktemp -d /tmp/push-guard-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# TEST-ONLY- prefix = uniform fake-fixture form recognized by secret-sweep
# cat7 (DGN-1058).
FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=TEST-ONLY-token123
ALLOWED_USER_IDS=12345
ENV_EOF

CURL_ARGS_LOG="$TMP/curl-args.log"
STUB="$TMP/bin"
mkdir -p "$STUB"
cat > "$STUB/curl" <<EOF
#!/bin/bash
printf '%s\n' "\$@" >> "$CURL_ARGS_LOG"
_ofile=""
prev=""
for a in "\$@"; do
  if [[ "\$prev" == "-o" ]]; then _ofile="\$a"; fi
  prev="\$a"
done
[[ -n "\$_ofile" ]] && echo '{"ok":true}' > "\$_ofile"
echo "200"
EOF
chmod +x "$STUB/curl"

echo "== DGN-1122 push.sh sender-side test-context guard =="

# --- 1. POSITIVE SELF-VERIFICATION: firing IS detectable (guard bypassed) ---
: > "$CURL_ARGS_LOG"
rc=0
OUT="$(PATH="$STUB:$PATH" PUSH_GUARD_OVERRIDE=send-anyway \
  bash "$PUSH_SH" --env "$FAKE_ENV" --text "fire-visibility probe" 2>&1)" || rc=$?
assert_eq "1: bypassed guard -> push exits 0" "$rc" "0"
ARGS="$(cat "$CURL_ARGS_LOG" 2>/dev/null || true)"
assert_contains "1: firing DETECTED by stub (sendMessage hit)" "$ARGS" "sendMessage"
assert_contains "1: fired payload visible to detector" "$ARGS" "fire-visibility probe"
assert_contains "1: override left a receipt" "$OUT" "GUARD OVERRIDE receipt"
assert_contains "1: receipt names the evidence" "$OUT" "test-runner ancestor"

# --- 2. guard on, ancestry layer: this tests/-path harness must be caught ---
: > "$CURL_ARGS_LOG"
rc=0
OUT="$(PATH="$STUB:$PATH" \
  bash "$PUSH_SH" --env "$FAKE_ENV" --text "must never leave" 2>&1)" || rc=$?
assert_eq "2: blocked -> exit 3" "$rc" "3"
assert_contains "2: LOUD refusal" "$OUT" "BLOCKED (DGN-1122 test-context guard)"
assert_contains "2: evidence named (ancestry)" "$OUT" "test-runner ancestor"
assert_contains "2: payload named" "$OUT" "must never leave"
ARGS="$(cat "$CURL_ARGS_LOG" 2>/dev/null || true)"
assert_eq "2: ZERO curl calls (nothing fired)" "$ARGS" ""

# --- 3. guard on, env-marker layer (checked before ancestry) ---
: > "$CURL_ARGS_LOG"
rc=0
OUT="$(PATH="$STUB:$PATH" PYTEST_CURRENT_TEST="tests/test_x.py::test_y (call)" \
  bash "$PUSH_SH" --env "$FAKE_ENV" --text "pytest leak probe" 2>&1)" || rc=$?
assert_eq "3: blocked -> exit 3" "$rc" "3"
assert_contains "3: evidence names the runner marker" "$OUT" "PYTEST_CURRENT_TEST"
ARGS="$(cat "$CURL_ARGS_LOG" 2>/dev/null || true)"
assert_eq "3: ZERO curl calls (nothing fired)" "$ARGS" ""

# --- 4. FALSE-POSITIVE ZERO: routine-shaped caller passes untouched ---
# A legit routine runs from a non-tests path with launchd/cron ancestry and
# no runner env markers. Reproduce that shape: a wrapper in /tmp, spawned via
# double-fork so it reparents to init/launchd (the test harness disappears
# from its ancestry), with runner markers explicitly unset.
LEGIT_DIR="$(mktemp -d /tmp/push-guard-legit.XXXXXX)"
WRAP="$LEGIT_DIR/legit-routine.sh"
WRAP_OUT="$LEGIT_DIR/out.log"
WRAP_RC="$LEGIT_DIR/rc"
cat > "$WRAP" <<EOF
#!/bin/bash
unset PYTEST_CURRENT_TEST PYTEST_VERSION BATS_TEST_FILENAME PUSH_GUARD_OVERRIDE
sleep 1  # let the spawning subshell die -> reparent to init/launchd
PATH="$STUB:\$PATH" "$PUSH_SH" --env "$FAKE_ENV" --text "legit routine push" \
  > "$WRAP_OUT" 2>&1
echo "\$?" > "$WRAP_RC"
EOF
chmod +x "$WRAP"
: > "$CURL_ARGS_LOG"
( bash "$WRAP" & )   # subshell exits immediately; wrapper reparents
_waited=0
while [[ ! -s "$WRAP_RC" && "$_waited" -lt 30 ]]; do
  sleep 0.5
  _waited=$(( _waited + 1 ))
done
if [[ ! -s "$WRAP_RC" ]]; then
  fail "4: legit wrapper never finished (timeout)"
else
  OUT="$(cat "$WRAP_OUT")"
  assert_eq "4: legit routine push exits 0 (NOT blocked)" "$(cat "$WRAP_RC")" "0"
  assert_not_contains "4: no guard block output" "$OUT" "BLOCKED"
  assert_not_contains "4: no override receipt (guard never triggered)" "$OUT" "OVERRIDE"
  ARGS="$(cat "$CURL_ARGS_LOG" 2>/dev/null || true)"
  assert_contains "4: push actually fired" "$ARGS" "legit routine push"
fi
rm -rf "$LEGIT_DIR"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
