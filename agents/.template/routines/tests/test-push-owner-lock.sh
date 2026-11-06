#!/bin/bash
# test-lane: fast
# Claimed-owner recipient regression. All sends use a curl stub, and all
# state lives in a temporary instance (including the real ownership reader).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TMP="$(mktemp -d /tmp/push-owner-lock-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
INST="$TMP/inst"
mkdir -p "$INST/routines" "$INST/bridge" "$INST/.telegram_bot" "$INST/config" "$TMP/bin"
for dir in routines bridge; do
  for entry in "$HOST_ROOT/$dir"/*; do
    [[ "$(basename "$entry")" == __pycache__ ]] && continue
    ln -s "$entry" "$INST/$dir/$(basename "$entry")"
  done
done
ENV_FILE="$INST/.telegram_bot/.env"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-owner\nALLOWED_USER_IDS=\n' > "$ENV_FILE"
printf '12345\n' > "$INST/.telegram_bot/owner.lock"

cat > "$TMP/bin/curl" <<'CURL'
#!/bin/bash
printf '%s\n' "$@" >> "$CURL_LOG"
out=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == -o ]]; then out="$2"; shift 2; else shift; fi
done
[[ -n "$out" ]] && printf '%s' '{"ok":true,"result":{"message_id":77}}' > "$out"
printf 200
CURL
chmod +x "$TMP/bin/curl"

PASS=0
eq() {
  if [[ "$2" != "$3" ]]; then
    printf 'FAIL: %s: got %s, wanted %s\n%s\n' "$1" "$2" "$3" "$OUT"
    exit 1
  fi
  echo "PASS: $1"
  PASS=$((PASS + 1))
}
run_push() {
  : > "$TMP/curl.log"
  RC=0
  OUT="$(PATH="$TMP/bin:$PATH" CURL_LOG="$TMP/curl.log" \
    PUSH_GUARD_OVERRIDE=send-anyway PYTHONDONTWRITEBYTECODE=1 \
    bash "$INST/routines/push.sh" --audience owner --text 'Ready to begin' "$@" 2>&1)" || RC=$?
}
chat() { sed -n 's/^chat_id=//p' "$TMP/curl.log"; }

# Default INSTANCE_ENV path: both routine sends and strict onboarding screens.
run_push
eq 'claimed owner routine rc' "$RC" 0
eq 'claimed owner routine chat' "$(chat)" 12345
run_push --strict --buttons-json '[[{"text":"Start","callback_data":"opt:start"}]]'
eq 'claimed owner screen rc' "$RC" 0
eq 'claimed owner screen chat' "$(chat)" 12345
grep -q 'inline_keyboard' "$TMP/curl.log"
eq 'strict receipt reports owner chat' "$(printf '%s\n' "$OUT" | tail -1)" '{"ok":true,"message_id":77,"chat_id":12345}'

printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-owner\nALLOWED_USER_IDS=222,333\n' > "$ENV_FILE"
run_push
eq 'allowlist takes precedence' "$(chat)" 222
run_push --strict --chat 444
eq 'explicit chat takes precedence' "$(chat)" 444
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-owner\nALLOWED_USER_IDS=\n' > "$ENV_FILE"
run_push --strict --chat 444
eq 'explicit chat overrides owner lock' "$(chat)" 444

# A selected peer env must use that peer's lock, never the local owner.
PEER="$TMP/peer"
mkdir -p "$PEER/bridge" "$PEER/.telegram_bot"
cp "$HOST_ROOT/bridge/ownership.py" "$PEER/bridge/ownership.py"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-peer\nALLOWED_USER_IDS=\n' > "$PEER/.telegram_bot/.env"
printf '999\n' > "$PEER/.telegram_bot/owner.lock"
run_push --env "$PEER/.telegram_bot/.env"
eq 'explicit env rc' "$RC" 0
eq 'explicit env uses peer owner' "$(chat)" 999
printf 'STEWARD_ENV_FILE=%s\n' "$PEER/.telegram_bot/.env" > "$INST/config/agent.conf"
run_push --audience operator
eq 'claimed steward rc' "$RC" 0
eq 'claimed steward owner' "$(chat)" 999
rm "$PEER/.telegram_bot/owner.lock"
run_push --audience operator
eq 'unclaimed steward does not borrow local owner' "$RC" 1
eq 'unclaimed steward sends nothing' "$(cat "$TMP/curl.log")" ''

for state in missing malformed; do
  if [[ "$state" == missing ]]; then
    rm "$INST/.telegram_bot/owner.lock"
  else
    printf 'invalid\n' > "$INST/.telegram_bot/owner.lock"
  fi
  run_push
  eq "$state owner fails with config rc" "$RC" 1
  eq "$state owner sends nothing" "$(cat "$TMP/curl.log")" ''
  [[ "$OUT" == *'no chat id found (use /claim or set ALLOWED_USER_IDS'* ]]
done

echo "RESULT: pass=$PASS fail=0"
