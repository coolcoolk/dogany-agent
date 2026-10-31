#!/bin/bash
# test-lane: fast
# test-push-audience-route.sh -- DGN-1585 S2: where does an --audience
# operator send actually GO, and what does the ledger say it did.
#
# The regression this pins: push.sh let a caller's --env beat the steward
# resolved from config/agent.conf. Every repo caller that declares operator
# with --env (cron-guard's lease / terminal-state / final alert sites) passes
# its OWN instance .env, so those alarms reached the owner's chat while the
# outbound ledger recorded audience=operator. Case 1 fails on that code.
#
# Sandbox + stubbed curl, same pattern as test-dgn1528-outbound-record.sh:
# no real Telegram send, python3 real so the ledger writer runs for real.
#
# Run: bash routines/tests/test-push-audience-route.sh

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
assert_contains() {
  if [[ "$2" == *"$3"* ]]; then ok "$1"; else fail "$1 -- missing '$3' in: $2"; fi
}
assert_not_contains() {
  if [[ "$2" != *"$3"* ]]; then ok "$1"; else fail "$1 -- unexpected '$3' in: $2"; fi
}

TMP="$(mktemp -d /tmp/push-audience-route-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

INST="$TMP/inst"
link_entries() { # link_entries <real-dir> <sandbox-dir>
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
mkdir -p "$INST/config"
PUSH_SH="$INST/routines/push.sh"
CONF="$INST/config/agent.conf"
LEDGER="$INST/.telegram_bot/outbound-ledger.jsonl"

# Two distinguishable channels. TEST-ONLY- prefix = secret-sweep fake form.
OWN_ENV="$TMP/own.env"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-owntoken\nALLOWED_USER_IDS=111\n' > "$OWN_ENV"
STEWARD_ENV="$TMP/steward.env"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-stewardtoken\nALLOWED_USER_IDS=999\n' > "$STEWARD_ENV"

STUB="$TMP/bin"
mkdir -p "$STUB"
CURL_LOG="$TMP/curl.log"
cat > "$STUB/curl" <<'CURL_EOF'
#!/bin/bash
OUT=""
ARGS=("$@")
for ((i=0;i<${#ARGS[@]};i++)); do
  [[ "${ARGS[$i]}" == "-o" ]] && OUT="${ARGS[$((i+1))]}"
done
printf '%s\n' "$*" >> "$CURL_LOG"
[[ -n "$OUT" ]] && echo '{"ok":true,"result":{"message_id":1}}' > "$OUT"
printf '200'
CURL_EOF
chmod +x "$STUB/curl"

# run_push <args...> -> combined stdout/stderr + PUSH_RC=<rc>; curl log reset.
run_push() {
  : > "$CURL_LOG"
  PATH="$STUB:$PATH" CURL_LOG="$CURL_LOG" PUSH_GUARD_OVERRIDE=send-anyway \
    DOGANY_MACHINE_STEWARD_FILE="${STEWARD_MARKER:-$TMP/no-steward-marker}" \
    bash "$PUSH_SH" "$@" 2>&1
  echo "PUSH_RC=$?"
}
sent_to() { # which bot token the stub saw on sendMessage
  grep -o 'bot[^/]*/sendMessage' "$CURL_LOG" | head -1 | sed 's#/sendMessage##; s#^bot##'
}
last_audience() {
  tail -n1 "$LEDGER" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d.get("audience",""), d.get("chat_id",""))'
}

echo "== DGN-1585 S2 push.sh audience routing =="

# --- 1. operator + steward resolved + caller --env names its OWN instance:
#        the steward wins, the override is logged, the ledger tells the truth.
printf 'STEWARD_ENV_FILE=%s\n' "$STEWARD_ENV" > "$CONF"
OUT="$(run_push --text "lease alarm" --env "$OWN_ENV" --audience operator)"
assert_contains "1: send succeeds" "$OUT" "PUSH_RC=0"
assert_eq "1: sent with the steward token, not the caller's --env" "$(sent_to)" "TEST-ONLY-stewardtoken"
assert_contains "1: the --env override is logged, not silent" "$OUT" "overridden by STEWARD_ENV_FILE"
assert_eq "1: ledger row = operator to the steward chat" "$(last_audience)" "operator 999"

# --- 2. operator + steward resolved, no --env -> steward, no override line.
OUT="$(run_push --text "pack lag" --audience operator)"
assert_contains "2: send succeeds" "$OUT" "PUSH_RC=0"
assert_eq "2: sent with the steward token" "$(sent_to)" "TEST-ONLY-stewardtoken"
assert_not_contains "2: no override line when --env was absent" "$OUT" "overridden"
assert_eq "2: ledger row = operator to the steward chat" "$(last_audience)" "operator 999"

# --- 3. operator + --env equal to the steward (bot.py / notice_spool shape):
#        no spurious override line.
OUT="$(run_push --text "machine line" --env "$STEWARD_ENV" --audience operator)"
assert_eq "3: sent with the steward token" "$(sent_to)" "TEST-ONLY-stewardtoken"
assert_not_contains "3: no override line when --env already is the steward" "$OUT" "overridden"

# --- 4. operator + steward UNRESOLVED (path missing): falls back to the
#        caller's own channel (its owner), one loud line, send still succeeds.
printf 'STEWARD_ENV_FILE=%s\n' "$TMP/nope.env" > "$CONF"
OUT="$(run_push --text "lease alarm" --env "$OWN_ENV" --audience operator)"
assert_contains "4: send still succeeds (never a silent drop)" "$OUT" "PUSH_RC=0"
assert_contains "4: unresolved fallback logged" "$OUT" "audience=operator unresolved -> owner"
assert_eq "4: sent with the caller's own token" "$(sent_to)" "TEST-ONLY-owntoken"
assert_eq "4: ledger row = owner (who actually got it)" "$(last_audience)" "owner 111"

# --- 5. owner audience is untouched by a configured steward.
printf 'STEWARD_ENV_FILE=%s\n' "$STEWARD_ENV" > "$CONF"
OUT="$(run_push --text "reminder" --env "$OWN_ENV" --audience owner)"
assert_eq "5: owner send uses the caller's --env" "$(sent_to)" "TEST-ONLY-owntoken"
assert_not_contains "5: no audience log lines on the owner path" "$OUT" "audience=operator"
assert_eq "5: ledger row = owner" "$(last_audience)" "owner 111"

# --- 6. DGN-1808 r2 (dec-220): --no-owner-fallback + steward UNRESOLVED:
#        nothing is sent, one loud line, rc 9, no ledger row (nobody got it).
printf 'STEWARD_ENV_FILE=%s\n' "$TMP/nope.env" > "$CONF"
_rows_before="$(wc -l < "$LEDGER" | tr -d ' ')"
OUT="$(run_push --text "evidence finding" --env "$OWN_ENV" --audience operator --no-owner-fallback)"
assert_contains "6: rc 9 (not sent by design)" "$OUT" "PUSH_RC=9"
assert_contains "6: the log-only line is printed" "$OUT" "audience=operator unresolved -> not sent"
assert_not_contains "6: no owner fallback line" "$OUT" "unresolved -> owner"
assert_eq "6: no sendMessage at all" "$(sent_to)" ""
assert_eq "6: no ledger row appended" "$(wc -l < "$LEDGER" | tr -d ' ')" "$_rows_before"

# --- 7. --no-owner-fallback + steward resolved: a normal operator send.
printf 'STEWARD_ENV_FILE=%s\n' "$STEWARD_ENV" > "$CONF"
OUT="$(run_push --text "evidence finding" --audience operator --no-owner-fallback)"
assert_contains "7: send succeeds" "$OUT" "PUSH_RC=0"
assert_eq "7: sent with the steward token" "$(sent_to)" "TEST-ONLY-stewardtoken"
assert_eq "7: ledger row = operator" "$(last_audience)" "operator 999"

# --- 8. the flag on an owner send is a contradiction -> refused (rc 1).
OUT="$(run_push --text "reminder" --env "$OWN_ENV" --audience owner --no-owner-fallback)"
assert_contains "8: refused like a bad arg" "$OUT" "PUSH_RC=1"
assert_contains "8: says why" "$OUT" "requires --audience operator"
assert_eq "8: nothing sent" "$(sent_to)" ""

# --- 9. default unchanged: without the flag, unresolved still falls back.
printf 'STEWARD_ENV_FILE=\n' > "$CONF"
OUT="$(run_push --text "lease alarm" --env "$OWN_ENV" --audience operator)"
assert_contains "9: default fallback intact" "$OUT" "audience=operator unresolved -> owner"
assert_eq "9: sent with the caller's own token" "$(sent_to)" "TEST-ONLY-owntoken"

# --- DGN-1959: operator unresolved + the owner is an EXTERNAL user -> never
#     the owner's chat; operator log (+ steward inbox when designated), rc 9.
#     Shape measured 2026-10-09: a PoC handed to an outsider via /claim, so
#     its .env carries no ALLOWED_USER_IDS and owner.lock names the user.
EXT_BD="$TMP/ext/.telegram_bot"
mkdir -p "$EXT_BD"
ln -s "$(cd -P "$ROUTINES_DIR/.." && pwd)/bridge" "$TMP/ext/bridge"   # owner.lock is read via ownership.py
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-exttoken\n' > "$EXT_BD/.env"
printf '555\n' > "$EXT_BD/owner.lock"
: > "$EXT_BD/.claimed"
OPLOG="$INST/.telegram_bot/logs/operator-push.log"
printf 'STEWARD_ENV_FILE=\n' > "$CONF"

# --- 10. claimed install, no steward marker (rule 3) -> diverted.
_rows_before="$(wc -l < "$LEDGER" | tr -d ' ')"
OUT="$(run_push --text "roller failed" --env "$EXT_BD/.env" --audience operator)"
assert_contains "10: rc 9 (not sent by design)" "$OUT" "PUSH_RC=9"
assert_contains "10: says why" "$OUT" "owner is external (claimed install"
assert_eq "10: no sendMessage to the external owner" "$(sent_to)" ""
assert_eq "10: no ledger row" "$(wc -l < "$LEDGER" | tr -d ' ')" "$_rows_before"
assert_contains "10: operator log carries the alert" "$(cat "$OPLOG" 2>/dev/null)" "roller failed"

# --- 11. steward marker names the operator instance whose owner differs
#         (rule 2) -> diverted AND queued in the steward's session inbox,
#         even without the claim markers.
rm -f "$EXT_BD/.claimed" "$EXT_BD/owner.lock"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-exttoken\nALLOWED_USER_IDS=555\n' > "$EXT_BD/.env"
STW="$TMP/steward"; mkdir -p "$STW/.telegram_bot"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-stewardowntoken\nALLOWED_USER_IDS=111\n' > "$STW/.telegram_bot/.env"
printf '# steward\n%s\n' "$STW" > "$TMP/marker"
OUT="$(STEWARD_MARKER="$TMP/marker" run_push --text "roller failed again" --env "$EXT_BD/.env" --audience operator)"
assert_contains "11: rc 9" "$OUT" "PUSH_RC=9"
assert_contains "11: steward queue named" "$OUT" "steward inbox"
assert_eq "11: nothing sent" "$(sent_to)" ""
assert_contains "11: note landed in the steward inbox" "$(cat "$STW"/.telegram_bot/session-inbox/operator-push-*.md 2>/dev/null)" "roller failed again"

# --- 12. same marker, owner == the steward's owner (the operator's own PoC)
#         -> pre-DGN-1959 owner fallback, delivered.
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-exttoken\nALLOWED_USER_IDS=111\n' > "$EXT_BD/.env"
OUT="$(STEWARD_MARKER="$TMP/marker" run_push --text "roller failed" --env "$EXT_BD/.env" --audience operator)"
assert_contains "12: delivered" "$OUT" "PUSH_RC=0"
assert_eq "12: to the operator-owned instance's owner" "$(sent_to)" "TEST-ONLY-exttoken"

# --- 13. OWNER_KIND=external wins over everything (rule 1); owner audience
#         is untouched by it.
printf 'STEWARD_ENV_FILE=\nOWNER_KIND=external\n' > "$CONF"
OUT="$(STEWARD_MARKER="$TMP/marker" run_push --text "roller failed" --env "$EXT_BD/.env" --audience operator)"
assert_contains "13: explicit external -> rc 9" "$OUT" "PUSH_RC=9"
assert_contains "13: reason names the key" "$OUT" "OWNER_KIND=external"
assert_eq "13: nothing sent" "$(sent_to)" ""
OUT="$(run_push --text "your reminder" --env "$EXT_BD/.env" --audience owner)"
assert_contains "13: owner audience still delivered" "$OUT" "PUSH_RC=0"

# --- 14. OWNER_KIND=operator overrides the claim markers (rule 1).
printf 'STEWARD_ENV_FILE=\nOWNER_KIND=operator\n' > "$CONF"
printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-exttoken\n' > "$EXT_BD/.env"
printf '555\n' > "$EXT_BD/owner.lock"
OUT="$(run_push --text "roller failed" --env "$EXT_BD/.env" --audience operator)"
assert_contains "14: explicit operator-owned -> fallback delivered" "$OUT" "PUSH_RC=0"
assert_eq "14: to the owner" "$(sent_to)" "TEST-ONLY-exttoken"

echo
echo "RESULT: pass=$PASS fail=$FAIL"
[[ "$FAIL" -eq 0 ]]
