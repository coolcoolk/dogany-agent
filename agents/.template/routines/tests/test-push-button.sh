#!/bin/bash
# test-push-button.sh -- DGN-835 push.sh --button inline-keyboard tests.
#
# No real Telegram send: curl is stubbed and its argv captured.
#   1. --text + --button "label::cb" -> sendMessage carries reply_markup with
#      ONE inline button (correct JSON, Korean label survives).
#   2. no --button -> no reply_markup field (legacy behavior untouched).
#   3. malformed --button (no '::') -> warn + send WITHOUT button (push never dies).
#
# Run: bash routines/tests/test-push-button.sh
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
  if [[ "$hay" != *"$needle"* ]]; then ok "$desc"; else fail "$desc -- unexpected '$needle'"; fi
}

TMP="$(mktemp -d /tmp/push-button-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=fake:token123
ALLOWED_USER_IDS=12345
ENV_EOF

CURL_ARGS_LOG="$TMP/curl-args.log"
STUB="$TMP/bin"
mkdir -p "$STUB"
cat > "$STUB/curl" <<EOF
#!/bin/bash
printf '%s\n' "\$@" > "$CURL_ARGS_LOG"
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

run_push() {
  PATH="$STUB:$PATH" bash "$PUSH_SH" --env "$FAKE_ENV" "$@" 2>&1
  echo "PUSH_RC=$?"
}

echo "== DGN-835 push.sh --button =="

# --- 1. button present -> reply_markup with one inline button ---
OUT="$(run_push --text "defer notice" --button "지금 실행::usageretry:com.test.label")"
assert_contains "button: push succeeds" "$OUT" "PUSH_RC=0"
ARGS="$(cat "$CURL_ARGS_LOG")"
assert_contains "button: reply_markup field sent" "$ARGS" "reply_markup="
assert_contains "button: korean label survives" "$ARGS" '"text": "지금 실행"'
assert_contains "button: callback_data intact (:: split once)" "$ARGS" '"callback_data": "usageretry:com.test.label"'
assert_contains "button: single inline_keyboard row" "$ARGS" '"inline_keyboard": [[{'

# --- 2. no button -> no reply_markup (legacy path untouched) ---
: > "$CURL_ARGS_LOG"
OUT="$(run_push --text "plain notice")"
assert_contains "plain: push succeeds" "$OUT" "PUSH_RC=0"
ARGS="$(cat "$CURL_ARGS_LOG")"
assert_not_contains "plain: no reply_markup field" "$ARGS" "reply_markup="

# --- 3. malformed button -> warn, send without button, still rc0 ---
: > "$CURL_ARGS_LOG"
OUT="$(run_push --text "notice" --button "no-separator-here")"
assert_contains "malformed: push still succeeds" "$OUT" "PUSH_RC=0"
assert_contains "malformed: warn emitted" "$OUT" "invalid --button"
ARGS="$(cat "$CURL_ARGS_LOG")"
assert_not_contains "malformed: sent without reply_markup" "$ARGS" "reply_markup="

# --- 4. callback_data 64-BYTE guard (DGN-835 MAJOR-발견3) ---
# Telegram hard-limits callback_data to 64 BYTES; an oversize value 400s the
# send and (pre-fix) killed the WHOLE notification. The guard drops only the
# button; the text must always go out.
CB63="usageretry:$(printf 'a%.0s' $(seq 1 52))"   # 11 + 52 = 63 bytes
CB64="${CB63}a"                                    # exactly 64 bytes
CB65="${CB64}a"                                    # 65 bytes -> drop button
: > "$CURL_ARGS_LOG"
OUT="$(run_push --text "boundary ok" --button "지금 실행::${CB64}")"
assert_contains "64B exact: push succeeds" "$OUT" "PUSH_RC=0"
ARGS="$(cat "$CURL_ARGS_LOG")"
assert_contains "64B exact: button kept (boundary inclusive)" "$ARGS" "reply_markup="

: > "$CURL_ARGS_LOG"
OUT="$(run_push --text "defer notice must survive" --button "지금 실행::${CB65}")"
assert_contains "65B: push still succeeds" "$OUT" "PUSH_RC=0"
assert_contains "65B: oversize warn emitted" "$OUT" "exceeds 64 bytes"
ARGS="$(cat "$CURL_ARGS_LOG")"
assert_not_contains "65B: button dropped" "$ARGS" "reply_markup="
assert_contains "65B: notification TEXT still sent" "$ARGS" "text=defer notice must survive"

# byte-vs-char: 22 Korean chars = 66 BYTES (utf-8) though only 22 chars --
# the guard must count BYTES, not characters.
CB_KR="가가가가가가가가가가가가가가가가가가가가가가"
: > "$CURL_ARGS_LOG"
OUT="$(run_push --text "multibyte notice" --button "라벨::${CB_KR}")"
assert_contains "multibyte: push still succeeds" "$OUT" "PUSH_RC=0"
assert_contains "multibyte: byte-length guard fired" "$OUT" "exceeds 64 bytes"
ARGS="$(cat "$CURL_ARGS_LOG")"
assert_not_contains "multibyte: button dropped (66 bytes > 64)" "$ARGS" "reply_markup="

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
