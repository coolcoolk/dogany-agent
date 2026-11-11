#!/bin/bash
# test-lane: fast
# test-push-edit-dgn1803.sh -- push.sh --capabilities and --strict --edit.
# No real Telegram send: curl is stubbed (argv captured, reply steered by
# STUB_CODE / STUB_BODY / STUB_CURL_RC). Sandbox instance pattern as in
# test-push-button.sh (writes land in $TMP, never in the host tree).
# Run: bash routines/tests/test-push-edit-dgn1803.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_ROOT="$(cd "$ROUTINES_DIR/.." && pwd)"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
eq()   { if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 -- got '$2' want '$3'"; fi; }
has()  { if [[ "$2" == *"$3"* ]]; then ok "$1"; else fail "$1 -- missing '$3' in: $2"; fi; }
hasnt(){ if [[ "$2" != *"$3"* ]]; then ok "$1"; else fail "$1 -- unexpected '$3'"; fi; }

TMP="$(mktemp -d /tmp/push-edit-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
FAKE_ENV="$TMP/fake.env"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-token123\nALLOWED_USER_IDS=12345\n' > "$FAKE_ENV"

INST="$TMP/inst"
link_entries() {
  local src="$1" dst="$2" e name
  mkdir -p "$dst"
  for e in "$src"/* "$src"/.[!.]*; do
    if [ -e "$e" ] || [ -L "$e" ]; then
      name="$(basename "$e")"
      case "$name" in __pycache__|.pytest_cache) continue ;; esac
      ln -s "$e" "$dst/$name"
    fi
  done
}
link_entries "$ROUTINES_DIR"     "$INST/routines"
link_entries "$HOST_ROOT/bridge" "$INST/bridge"
PUSH_SH="$INST/routines/push.sh"
LEDGER="$INST/.telegram_bot/outbound-ledger.jsonl"

ARGS_LOG="$TMP/curl-args.log"; CALLS_LOG="$TMP/curl-calls.log"
STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/curl" <<CURL_EOF
#!/bin/bash
echo call >> "$CALLS_LOG"
printf '%s\n' "\$@" >> "$ARGS_LOG"
OUT=""; ARGS=("\$@")
for ((i=0;i<\${#ARGS[@]};i++)); do [[ "\${ARGS[\$i]}" == "-o" ]] && OUT="\${ARGS[\$((i+1))]}"; done
[[ "\${STUB_CURL_RC:-0}" != "0" ]] && exit "\$STUB_CURL_RC"
printf '%s' "\$STUB_BODY" > "\$OUT"
printf '%s' "\${STUB_CODE:-200}"
CURL_EOF
chmod +x "$STUB/curl"

# run_push <http-code> <stub-body|-> <curl-rc> <push args...>; sets OUT ERR RC
run_push() {
  : > "$ARGS_LOG"; : > "$CALLS_LOG"
  local code="$1" body="$2" crc="$3"; shift 3
  [[ "$body" == "-" ]] && body='{"ok":true,"result":{"message_id":77}}'
  OUT="$(STUB_CODE="$code" STUB_BODY="$body" STUB_CURL_RC="$crc" PATH="$STUB:$PATH" \
    PUSH_GUARD_OVERRIDE=send-anyway bash "$PUSH_SH" --env "$FAKE_ENV" "$@" 2>"$TMP/err")"; RC=$?
  ERR="$(cat "$TMP/err")"
}
ledger_rows() { [[ -f "$LEDGER" ]] && wc -l < "$LEDGER" | tr -d ' ' || echo 0; }
KB='[[{"text":"1. a","callback_data":"opt:a"}]]'

echo "== capability line =="
OUT="$(bash "$PUSH_SH" --capabilities 2>&1)"; RC=$?
eq "capabilities rc 0" "$RC" 0
eq "capabilities one line" "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" 1
[[ "$OUT" =~ (^|[[:space:]])edit($|[[:space:]]) ]] && ok "capabilities lists word 'edit'" || fail "capabilities lacks edit: $OUT"

echo "== 0 edited =="
B=$(ledger_rows)
run_push 200 - 0 --strict --edit 77 --chat 555 --text "set table v2"
eq "rc 0" "$RC" 0
eq "stdout JSON" "$OUT" '{"ok":true,"message_id":77,"chat_id":555,"edited":true}'
ARGS="$(cat "$ARGS_LOG")"
has "editMessageText endpoint" "$ARGS" "/editMessageText"
hasnt "not sendMessage" "$ARGS" "/sendMessage"
has "message_id sent" "$ARGS" "message_id=77"
has "chat override sent" "$ARGS" "chat_id=555"
has "html parse mode" "$ARGS" "parse_mode=HTML"
hasnt "no keyboard when --buttons-json omitted (removes it)" "$ARGS" "reply_markup"
eq "ledger +1 row" "$(( $(ledger_rows) - B ))" 1
ROW="$(tail -n1 "$LEDGER")"
has "ledger tagged edit" "$ROW" '"tag": "edit"'
has "ledger carries message_id" "$ROW" '"message_id": "77"'
has "ledger body" "$ROW" "set table v2"

echo "== keyboard kept =="
run_push 200 - 0 --strict --edit 77 --text "set table v3" --buttons-json "$KB"
eq "rc 0 with keyboard" "$RC" 0
has "reply_markup sent" "$(cat "$ARGS_LOG")" "reply_markup="
has "inline_keyboard payload" "$(cat "$ARGS_LOG")" "inline_keyboard"

echo "== thread/silent not transmitted on edit =="
run_push 200 - 0 --strict --edit 77 --thread 9 --silent --text "t"
eq "rc 0 with --thread" "$RC" 0
hasnt "no message_thread_id" "$(cat "$ARGS_LOG")" "message_thread_id"
hasnt "no disable_notification" "$(cat "$ARGS_LOG")" "disable_notification"

echo "== plain send unchanged: no tag, sendMessage =="
B=$(ledger_rows)
run_push 200 - 0 --strict --text "fresh"
eq "send rc 0" "$RC" 0
eq "send JSON has no edited" "$OUT" '{"ok":true,"message_id":77,"chat_id":12345}'
has "sendMessage endpoint" "$(cat "$ARGS_LOG")" "/sendMessage"
hasnt "send row untagged" "$(tail -n1 "$LEDGER")" '"tag"'

echo "== 8 not modified =="
B=$(ledger_rows)
run_push 400 '{"ok":false,"error_code":400,"description":"Bad Request: message is not modified: specified new message content and reply markup are exactly the same"}' 0 \
  --strict --edit 77 --text "same"
eq "rc 8" "$RC" 8
has "stdout marks not_modified" "$OUT" '"not_modified":true'
eq "no ledger row" "$(( $(ledger_rows) - B ))" 0
eq "no plain retry on definite 400" "$(wc -l < "$CALLS_LOG" | tr -d ' ')" 1

echo "== 7 not editable (4 causes) =="
for d in "Bad Request: message to edit not found" \
         "Bad Request: message can't be edited" \
         "Bad Request: MESSAGE_ID_INVALID" \
         "Bad Request: message can't be edited: too old"; do
  B=$(ledger_rows)
  run_push 400 "{\"ok\":false,\"error_code\":400,\"description\":\"$d\"}" 0 --strict --edit 77 --text "x"
  eq "rc 7 for: $d" "$RC" 7
  eq "no ledger row ($d)" "$(( $(ledger_rows) - B ))" 0
  eq "no stdout ($d)" "$OUT" ""
done

echo "== 6 uncertain =="
for crc in 5 6 7 35; do
  run_push 000 - "$crc" --strict --edit 77 --text "x"
  eq "connect-phase curl $crc -> not delivered rc 2" "$RC" 2
  has "connect failure keeps curl rc" "$ERR" "curl rc=$crc"
  eq "connect failure produces no receipt" "$OUT" ""
done
B=$(ledger_rows)
run_push 000 - 28 --strict --edit 77 --text "x"
eq "transport error rc 6" "$RC" 6
run_push 200 'not json' 0 --strict --edit 77 --text "x"
eq "unparseable reply rc 6" "$RC" 6
run_push 502 '{"ok":false}' 0 --strict --edit 77 --text "x"
eq "5xx rc 6" "$RC" 6
eq "no ledger row on uncertain" "$(( $(ledger_rows) - B ))" 0

echo "== 2 other telegram rejection =="
run_push 400 '{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}' 0 --strict --edit 77 --text "x"
eq "rc 2" "$RC" 2
echo "== HTML parse failure still retries plain =="
run_push 400 '{"ok":false,"error_code":400,"description":"Bad Request: can'"'"'t parse entities: Unsupported start tag"}' 0 --strict --edit 77 --text "x"
eq "parse-entities 400 retried (2 calls)" "$(wc -l < "$CALLS_LOG" | tr -d ' ')" 2

echo "== strict-gate refusals (nothing sent) =="
run_push 200 - 0 --strict --edit 77 --text $'hello\nsignal::ping'
eq "machine-line gate rc 5" "$RC" 5
eq "machine-line: curl never called" "$(wc -l < "$CALLS_LOG" | tr -d ' ')" 0
run_push 200 - 0 --strict --edit 77 --text "$(printf 'x%.0s' $(seq 1 4100))"
eq "over-long rc 5" "$RC" 5
run_push 200 - 0 --strict --edit 77 --text "ok" --buttons-json '[[{"text":"a"}]]'
eq "bad buttons rc 5" "$RC" 5
run_push 200 - 0 --strict --edit 77 --text "[[IDRILL:d9660001]]"
eq "idrill marker rc 5" "$RC" 5
printf 'other' > "$TMP/exp"
run_push 200 - 0 --strict --edit 77 --text "mine" --expect-file "$TMP/exp"
eq "byte-compare mismatch rc 5" "$RC" 5
eq "refusals: curl never called" "$(wc -l < "$CALLS_LOG" | tr -d ' ')" 0
run_push 200 - 0 --strict --edit 77 --text "see /Users/someone/file.txt path dsp-20260930-171004-9945-dgn1803 run"
eq "identifier guard rc 4" "$RC" 4

echo "== test-context guard (no override) sends nothing =="
: > "$CALLS_LOG"
OUT="$(PYTEST_CURRENT_TEST=x PATH="$STUB:$PATH" bash "$PUSH_SH" --env "$FAKE_ENV" --strict --edit 77 --text hi 2>/dev/null)"; RC=$?
eq "guard rc 3" "$RC" 3
eq "guard: curl never called" "$(wc -l < "$CALLS_LOG" | tr -d ' ')" 0

echo "== argument errors rc 1 =="
run_push 200 - 0 --edit 77 --text "x"
eq "--edit without --strict rc 1" "$RC" 1
run_push 200 - 0 --strict --edit abc --text "x"
eq "non-numeric id rc 1" "$RC" 1
run_push 200 - 0 --strict --edit 0 --text "x"
eq "zero id rc 1" "$RC" 1
run_push 200 - 0 --strict --edit 77
eq "missing --text rc 1" "$RC" 1

echo
echo "passed=$PASS failed=$FAIL"
[[ "$FAIL" -eq 0 ]]
