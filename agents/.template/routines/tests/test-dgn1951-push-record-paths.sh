#!/bin/bash
# test-lane: fast
# test-dgn1951-push-record-paths.sh -- DGN-1951: every push.sh send point
# writes the outbound ledger, and every row carries the sent bubble's
# message_id, so the bridge can show the session what it sent (and map a
# Telegram reply-to back to the pushed bubble).
#
# Before DGN-1951 only the main text sendMessage recorded: a --photo send and
# the idrill keyboard bubble (often the very question the owner answers)
# left no row, and plain rows carried no message_id.
#
# Cases:
#   1  text send       -> one row, body + message_id from the response
#   2  --photo only    -> one row, body "[photo] <caption>", message_id
#   3  text + idrill   -> two rows: the body bubble and the keyboard bubble
#   4  failed photo    -> no row
#   5  DGN-2062: strict --buttons-json opt: keyboard -> row "keyboard" = texts
#   6  DGN-2062: --button with a non-opt callback -> no "keyboard" field
#   7  DGN-2062: strict --edit without buttons -> "keyboard" = [] (dropped)
#
# Sandbox instance + stubbed curl (DGN-1566 pattern, see
# test-push-idrill-exitcode.sh): no real send, no write into the host tree.
#
# Run: bash routines/tests/test-dgn1951-push-record-paths.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_ROOT="$(cd "$ROUTINES_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_eq() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 -- got='$2' want='$3'"; fi
}

TMP="$(mktemp -d /tmp/push-dgn1951-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=TEST-ONLY-token123
ALLOWED_USER_IDS=12345
ENV_EOF

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

ARM_DIR="$INST/files/program/.idrill-arm"
mkdir -p "$ARM_DIR"
ARM_ID="d1951001"
cat > "$ARM_DIR/$ARM_ID" <<'ARM_EOF'
{
  "session_id": "sess-dgn1951-test",
  "cmd": ["/bin/true"],
  "step_final": "1",
  "step_buttons": {"1": [["yes", "yes"], ["no", "no"]]},
  "step_text": {"1": "Commit it as is?"}
}
ARM_EOF

PHOTO="$TMP/p.png"
printf 'not-really-a-png' > "$PHOTO"

# Distinct message ids per bubble kind: body 501, keyboard 502, photo 503.
STUB="$TMP/bin"
mkdir -p "$STUB"
cat > "$STUB/curl" <<'CURL_EOF'
#!/bin/bash
OUT=""
ARGS=("$@")
for ((i=0;i<${#ARGS[@]};i++)); do
  if [[ "${ARGS[$i]}" == "-o" ]]; then OUT="${ARGS[$((i+1))]}"; fi
done
mid=501; code="${BODY_CODE:-200}"
for a in "${ARGS[@]}"; do
  [[ "$a" == reply_markup=* ]] && mid=502
  [[ "$a" == */sendPhoto ]] && { mid=503; code="${PHOTO_CODE:-200}"; }
done
# An edit answers with the edited message's own id.
for a in "${ARGS[@]}"; do
  [[ "$a" == message_id=* ]] && mid="${a#message_id=}"
done
if [[ "$code" == "200" ]]; then
  echo "{\"ok\":true,\"result\":{\"message_id\":$mid}}" > "$OUT"
else
  echo "{\"ok\":false,\"error_code\":$code,\"description\":\"stub failure\"}" > "$OUT"
fi
printf '%s' "$code"
CURL_EOF
chmod +x "$STUB/curl"

run_push() {
  PATH="$STUB:$PATH" PUSH_GUARD_OVERRIDE=send-anyway \
    bash "$PUSH_SH" --env "$FAKE_ENV" "$@" >/dev/null 2>&1
  echo "$?"
}
lines() { [[ -f "$LEDGER" ]] && wc -l < "$LEDGER" | tr -d ' ' || echo 0; }
row() { # row <n-from-end> <field>
  tail -n "$1" "$LEDGER" | head -n1 | python3 -c '
import json, sys
print(json.loads(sys.stdin.read()).get(sys.argv[1], ""))' "$2"
}

echo "== DGN-1951 push.sh: every send point records =="

# 1 -------------------------------------------------------------------------
assert_eq "text send rc" "$(run_push --text "Nightly sync done")" "0"
assert_eq "text send: one row" "$(lines)" "1"
assert_eq "text row body" "$(row 1 body)" "Nightly sync done"
assert_eq "text row message_id" "$(row 1 message_id)" "501"
assert_eq "text row is not an edit" "$(row 1 tag)" ""

# 2 -------------------------------------------------------------------------
assert_eq "photo send rc" "$(run_push --photo "$PHOTO" --caption "Chart for review")" "0"
assert_eq "photo send: one more row" "$(lines)" "2"
assert_eq "photo row body" "$(row 1 body)" "[photo] Chart for review"
assert_eq "photo row message_id" "$(row 1 message_id)" "503"
assert_eq "photo row chat" "$(row 1 chat_id)" "12345"

# 3 -------------------------------------------------------------------------
assert_eq "idrill send rc" "$(run_push --text "QA passed
[[IDRILL:$ARM_ID]]")" "0"
assert_eq "idrill send: body + keyboard rows" "$(lines)" "4"
assert_eq "body bubble row" "$(row 2 message_id)" "501"
assert_eq "keyboard bubble row body" "$(row 1 body)" "Commit it as is?"
assert_eq "keyboard bubble row message_id" "$(row 1 message_id)" "502"

# 4 -------------------------------------------------------------------------
before="$(lines)"
PHOTO_CODE=400 run_push --photo "$PHOTO" --caption "never sent" >/dev/null
assert_eq "failed photo: no row" "$(lines)" "$before"

kb() { # kb <n-from-end> -> the row's keyboard as JSON, "absent" when missing
  tail -n "$1" "$LEDGER" | head -n1 | python3 -c '
import json, sys
r = json.loads(sys.stdin.read())
print(json.dumps(r["keyboard"], ensure_ascii=False) if "keyboard" in r else "absent")'
}

# 5 -------------------------------------------------------------------------
assert_eq "strict opt keyboard rc" "$(run_push --strict --text "Ship which?" \
  --buttons-json '[[{"text":"1. Alpha","callback_data":"opt:1"}],[{"text":"2. Beta","callback_data":"opt:2"}]]')" "0"
assert_eq "strict opt keyboard row message_id" "$(row 1 message_id)" "502"
assert_eq "strict opt keyboard recorded" "$(kb 1)" '["1. Alpha", "2. Beta"]'

# 6 -------------------------------------------------------------------------
assert_eq "non-opt button rc" "$(run_push --text "Restart?" --button "Go::upd:r:1")" "0"
assert_eq "non-opt button: no keyboard field" "$(kb 1)" "absent"
assert_eq "plain text row: no keyboard field" "$(kb 7)" "absent"

# 7 -------------------------------------------------------------------------
run_push --strict --edit 502 --text "Ship which? (closed)" >/dev/null
assert_eq "edit without buttons: keyboard dropped" "$(kb 1)" "[]"
assert_eq "edit row message_id" "$(row 1 message_id)" "502"

echo
echo "passed=$PASS failed=$FAIL"
[[ "$FAIL" -eq 0 ]]
