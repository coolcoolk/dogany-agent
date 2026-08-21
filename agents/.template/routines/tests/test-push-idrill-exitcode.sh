#!/bin/bash
# test-push-idrill-exitcode.sh -- DGN-966 verification round: push.sh's
# keyboard-send-failure branch must exit 0, NEVER exit 2.
#
# Evidence that forced this (see push.sh comment at the KB_CODE branch):
# packs/health-trainer's handoff.consume "handler crash leaves the message
# for the next sweep" chain (redirect-respond.sh has no `|| true` around its
# push-gated.sh call) and the mirror-reconcile.sh/mirror-poll.sh
# STAMP-AFTER-PUSH pattern both treat ANY non-zero push.sh exit as "retry the
# whole push" -- no caller in this codebase distinguishes exit codes. If a
# keyboard-only failure (body already delivered) reused exit 2, either
# caller chain would RE-SEND THE SAME BODY on its next sweep/cycle. So:
#   - body sendMessage itself fails            -> exit 2 (unchanged, safe to
#     retry -- nothing was delivered)
#   - body OK, idrill keyboard sendMessage fails -> exit 0 + loud stderr WARN
#     (never silent, but never signals "whole push failed" either)
#
# No real Telegram send: curl is stubbed. The stub tells body-sendMessage
# calls (no reply_markup) apart from keyboard-sendMessage calls (has
# reply_markup) by scanning argv, and returns per-case codes.
#
# Run: bash routines/tests/test-push-idrill-exitcode.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$(cd "$ROUTINES_DIR/.." && pwd)"
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
  if [[ "$hay" != *"$needle"* ]]; then ok "$desc"; else fail "$desc -- unexpected '$needle'"; fi
}

TMP="$(mktemp -d /tmp/push-idrill-exitcode-test.XXXXXX)"

FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=fake:token123
ALLOWED_USER_IDS=12345
ENV_EOF

# Instance layout push.sh expects relative to itself: ../bridge (python hop
# resolves bridge.artifacts/bridge.formatting from the real template tree,
# no copy needed since we run push.sh in place) and ../files/program/.idrill-arm.
ARM_DIR="$TEMPLATE_DIR/files/program/.idrill-arm"
mkdir -p "$ARM_DIR"
ARM_ID="d9660001"   # IDRILL_ARM_ID_RE requires exactly 8 lowercase-hex chars
cat > "$ARM_DIR/$ARM_ID" <<'ARM_EOF'
{
  "session_id": "sess-exitcode-test",
  "cmd": ["/bin/true"],
  "step_final": "1",
  "step_buttons": {"1": [["8", "8"], ["9", "9"]]},
  "step_text": {"1": "reps?"}
}
ARM_EOF
trap 'rm -rf "$TMP"; rm -f "$ARM_DIR/$ARM_ID"' EXIT

# BODY_CODE / KB_CODE env vars steer the stub's two response codes.
STUB="$TMP/bin"
mkdir -p "$STUB"
cat > "$STUB/curl" <<'CURL_EOF'
#!/bin/bash
OUT=""
ARGS=("$@")
for ((i=0;i<${#ARGS[@]};i++)); do
  if [[ "${ARGS[$i]}" == "-o" ]]; then OUT="${ARGS[$((i+1))]}"; fi
done
IS_KB=0
for a in "${ARGS[@]}"; do
  [[ "$a" == reply_markup=* ]] && IS_KB=1
done
if [[ "$IS_KB" == "1" ]]; then
  code="${KB_CODE:-200}"
else
  code="${BODY_CODE:-200}"
fi
if [[ "$code" == "200" ]]; then
  echo '{"ok":true,"result":{"message_id":1}}' > "$OUT"
else
  echo "{\"ok\":false,\"error_code\":$code,\"description\":\"stub failure\"}" > "$OUT"
fi
printf '%s' "$code"
CURL_EOF
chmod +x "$STUB/curl"

run_push() {
  PATH="$STUB:$PATH" BODY_CODE="$BODY_CODE" KB_CODE="$KB_CODE" \
    bash "$PUSH_SH" --env "$FAKE_ENV" "$@" 2>&1
  echo "PUSH_RC=$?"
}

echo "== DGN-966 push.sh idrill keyboard-fail exit-code contract =="

# --- 1. body OK, keyboard OK -> exit 0, both messages sent ---
BODY_CODE=200 KB_CODE=200
OUT="$(run_push --text "본문
[[IDRILL:$ARM_ID]]")"
assert_contains "both ok: push succeeds" "$OUT" "PUSH_RC=0"
assert_contains "both ok: keyboard sent OK logged" "$OUT" "idrill keyboard sent OK"

# --- 2. body OK, keyboard send FAILS -> exit 0 (NOT 2), loud WARN, no
#        "whole push failed" signal a caller could retry on ---
BODY_CODE=200 KB_CODE=500
OUT="$(run_push --text "본문
[[IDRILL:$ARM_ID]]")"
assert_contains "kb fails: push STILL exits 0 (never re-signals as full failure)" "$OUT" "PUSH_RC=0"
assert_contains "kb fails: loud WARN present (never silent)" "$OUT" "WARN: idrill keyboard send failed"
assert_contains "kb fails: body-delivered context named in the warning" "$OUT" "body already delivered"
assert_not_contains "kb fails: does not say 'telegram failed' (that's the body-failure branch)" "$OUT" "[push] telegram failed"

# --- 3. body send itself FAILS (no idrill marker) -> exit 2 unchanged
#        (nothing delivered yet, still safe for a caller to retry) ---
BODY_CODE=403 KB_CODE=200
OUT="$(run_push --text "본문 (마커 없음)")"
assert_contains "body fails: push exits 2 (unchanged contract)" "$OUT" "PUSH_RC=2"
assert_contains "body fails: telegram failed logged" "$OUT" "telegram failed"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
