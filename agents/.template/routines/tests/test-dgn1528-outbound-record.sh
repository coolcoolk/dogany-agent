#!/bin/bash
# test-lane: fast    # measured 1.9s in place; resandboxed DGN-1568
# test-dgn1528-outbound-record.sh -- push.sh must leave a record of every
# outbound send it actually transmits, AT SEND TIME, via an append-only
# ledger (.telegram_bot/outbound-ledger.jsonl).
#
# DGN-1642: the old leg 2 -- a session-inbox drop
# (.telegram_bot/session-inbox/outbound-*.md) that handed the sending
# session a free follow-up turn on its own record -- is deleted at the
# producer. push.sh now never creates that turn on any send path; this
# suite asserts its ABSENCE on every case below, ledger presence unchanged.
#
# push.sh derives that path from ITS OWN location ($SCRIPT_DIR/../.telegram_bot),
# not from --env, matching the existing machine-line-alerts ledger's precedent
# (bridge/machine_gate.py MachineLineAlertLedger, same directory).
#
# DGN-1568: THIS SUITE USED TO RUN IN PLACE, driving push.sh straight out of
# the tree it lives in, so pre-DGN-1642 every one of the 3 successful sends
# below (this suite's own -- see cases 1/2/5; case 3 and the gated case 4
# send nothing) dropped a REAL outbound-record into the HOST tree's
# .telegram_bot/ -- measured 3 drops/run -- which a live agent's bridge then
# injected as its own history. DGN-1642 deletes that drop entirely, but the
# sandbox stays: the ledger assertions still need an isolated instance path
# (same sandbox pattern as routines/tests/test-push-idrill-exitcode.sh,
# DGN-1566 -- see that file's header for the full rationale). The sandbox is
# fresh and disposable (destroyed with $TMP), so the old backup/restore-the-
# host-ledger machinery is gone; the BASELINE-RELATIVE delta assertions stay
# for the ledger (multiple sends within THIS suite still share the sandbox's
# one ledger, so a later case must never be flaked by an earlier case's own
# row); the inbox assertions are now plain absence checks.
#
# No real Telegram send: curl is stubbed. python3 is the real interpreter
# (no stub) so the actual _record_outbound JSON/file-write code runs.
#
# Run: bash routines/tests/test-dgn1528-outbound-record.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_ROOT="$(cd "$ROUTINES_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}
assert_contains() {
  local desc="$1" hay="$2" needle="$3"
  if [[ "$hay" == *"$needle"* ]]; then ok "$desc"; else fail "$desc -- missing '$needle' in: $hay"; fi
}

TMP="$(mktemp -d /tmp/push-outbound-record-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# DGN-1568 sandbox instance (DGN-1566 pattern). One symlink per entry, so the
# sandbox dirs themselves are REAL and "$SCRIPT_DIR/.." lands inside the
# sandbox, not the host tree. __pycache__ is skipped on purpose.
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
PUSH_SH="$INST/routines/push.sh"

# push.sh writes the ledger/inbox to <sandbox>/.telegram_bot/ regardless of
# --env (see header note above) -- point --env only at a throwaway token
# file. The sandbox is fresh, so there is nothing to back up or restore: the
# ledger doesn't exist yet and the inbox starts empty.
INSTANCE_DIR="$INST/.telegram_bot"
LEDGER="$INSTANCE_DIR/outbound-ledger.jsonl"
INBOX="$INSTANCE_DIR/session-inbox"

BASELINE_LEDGER_LINES="0"
BASELINE_INBOX_LIST="$TMP/baseline_inbox.list"
: > "$BASELINE_INBOX_LIST"

# new_outbound_files_since_baseline <label> -- lists (one per line) the
# outbound-*.md files present now that were NOT in the baseline snapshot.
new_outbound_files_since_baseline() {
  find "$INBOX" -maxdepth 1 -type f -name 'outbound-*.md' 2>/dev/null | sort \
    | comm -13 "$BASELINE_INBOX_LIST" -
}

# TEST-ONLY- prefix = uniform fake-fixture form recognized by secret-sweep
# cat7 (DGN-1058); replaces the .sweepignore path allowlist for this file.
FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=TEST-ONLY-token123
ALLOWED_USER_IDS=12345
ENV_EOF

STUB="$TMP/bin"
mkdir -p "$STUB"
cat > "$STUB/curl" <<'CURL_EOF'
#!/bin/bash
OUT=""
ARGS=("$@")
for ((i=0;i<${#ARGS[@]};i++)); do
  if [[ "${ARGS[$i]}" == "-o" ]]; then OUT="${ARGS[$((i+1))]}"; fi
done
code="${BODY_CODE:-200}"
if [[ "$code" == "200" ]]; then
  echo '{"ok":true,"result":{"message_id":1}}' > "$OUT"
else
  echo "{\"ok\":false,\"error_code\":$code,\"description\":\"stub failure\"}" > "$OUT"
fi
printf '%s' "$code"
CURL_EOF
chmod +x "$STUB/curl"

run_push() {
  PATH="$STUB:$PATH" BODY_CODE="${BODY_CODE:-200}" \
    PUSH_GUARD_OVERRIDE=send-anyway \
    bash "$PUSH_SH" --env "$FAKE_ENV" "$@" 2>&1
  echo "PUSH_RC=$?"
}

echo "== DGN-1528 push.sh outbound-record contract (DGN-1642: ledger only) =="

# --- 1. a normal successful send appends exactly one ledger line, with the
#        right body and chat_id, and drops NO session-inbox file (DGN-1642:
#        leg 2 is deleted at the producer -- no turn is ever created) ---
BODY_CODE=200
OUT="$(run_push --text "hello owner, this went out")"
assert_contains "send succeeds" "$OUT" "PUSH_RC=0"

LINES_AFTER_1="$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')"
assert_eq "ledger grew by exactly 1 line after 1 send" \
  "$((LINES_AFTER_1 - BASELINE_LEDGER_LINES))" "1"

# The send just made appends LAST -- tail -n1 is deterministic regardless of
# what a sibling test wrote earlier in this same shared ledger.
LEDGER_LINE="$(tail -n1 "$LEDGER")"
LEDGER_CHECK="$(IB_LINE="$LEDGER_LINE" python3 -c '
import json, os
d = json.loads(os.environ["IB_LINE"])
assert d["chat_id"] == "12345", d
assert d["body"] == "hello owner, this went out", d
assert d["ts"], d
print("OK")
' 2>&1)"
assert_eq "ledger line has chat_id/body/ts" "$LEDGER_CHECK" "OK"

NEW_FILES_1="$(new_outbound_files_since_baseline)"
NEW_COUNT_1="$(printf '%s\n' "$NEW_FILES_1" | grep -c . || true)"
assert_eq "no outbound-*.md dropped in session-inbox (DGN-1642: no turn)" "${NEW_COUNT_1:-0}" "0"

# No stray dot-temp file left behind. DGN-1642: the session-inbox dir is
# never even created now (no leg 2), so this is vacuously true -- kept as a
# regression guard in case a session-inbox writer is ever reintroduced here.
STRAY_TMP="$(find "$INBOX" -maxdepth 1 -type f -name '.outbound-*.tmp' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "no leftover temp file (rename was atomic)" "$STRAY_TMP" "0"

# --- 2. a second send APPENDS (ledger is a log, not overwritten) and still
#        drops NO session-inbox file ---
OUT2="$(run_push --text "second message")"
assert_contains "second send succeeds" "$OUT2" "PUSH_RC=0"

LINES_AFTER_2="$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')"
assert_eq "ledger grew by exactly 2 lines after 2 sends (append-only)" \
  "$((LINES_AFTER_2 - BASELINE_LEDGER_LINES))" "2"
LEDGER_LINE_2="$(tail -n1 "$LEDGER")"
assert_contains "second ledger line carries the second body" "$LEDGER_LINE_2" "second message"

NEW_FILES_2="$(new_outbound_files_since_baseline)"
NEW_COUNT_2="$(printf '%s\n' "$NEW_FILES_2" | grep -c . || true)"
assert_eq "still no session-inbox drops after 2 sends" "${NEW_COUNT_2:-0}" "0"

# --- 3. a FAILED send (telegram rejects it) must NOT record anything --
# nothing was actually delivered, so there is nothing true to record.
LEDGER_LINES_BEFORE_FAIL="$(wc -l < "$LEDGER" | tr -d ' ')"
INBOX_COUNT_BEFORE_FAIL="$NEW_COUNT_2"
BODY_CODE=403
OUT3="$(run_push --text "this never goes out")"
assert_contains "failed send exits 2" "$OUT3" "PUSH_RC=2"
assert_eq "ledger unchanged after a failed send" \
  "$(wc -l < "$LEDGER" | tr -d ' ')" "$LEDGER_LINES_BEFORE_FAIL"
NEW_COUNT_3="$(new_outbound_files_since_baseline | grep -c . || true)"
assert_eq "no new session-inbox drop after a failed send" "${NEW_COUNT_3:-0}" "$INBOX_COUNT_BEFORE_FAIL"
FAIL_HITS="$(grep -c "this never goes out" "$LEDGER" 2>/dev/null || true)"
assert_eq "failed-send body never appears in the ledger" "${FAIL_HITS:-0}" "0"

# --- 4. a body that gates to EMPTY (DGN-1209 machine-line gate: a
#        registered ACTION_DROP token with no other content) never reaches
#        Telegram either -- nothing was sent, so nothing must be recorded.
#        This is the branch most likely to regress silently: it is easy to
#        wire _OUTBOUND_SENT="true" on a "success" (HTTP_CODE=200) path
#        without noticing that path never actually called _send_message. ---
BODY_CODE=200
LEDGER_LINES_BEFORE_GATED="$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')"
INBOX_COUNT_BEFORE_GATED="$(new_outbound_files_since_baseline | grep -c . || true)"
OUT4="$(run_push --text "DAY_SOURCE some=payload")"
assert_contains "machine-only body still exits 0 (DGN-1209 contract unchanged)" "$OUT4" "PUSH_RC=0"
assert_eq "no ledger growth on a machine-only (nothing-sent) body" \
  "$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')" "$LEDGER_LINES_BEFORE_GATED"
NEW_COUNT_4="$(new_outbound_files_since_baseline | grep -c . || true)"
assert_eq "no new session-inbox drop on a machine-only (nothing-sent) body" \
  "${NEW_COUNT_4:-0}" "${INBOX_COUNT_BEFORE_GATED:-0}"

# --- 5. the record must carry what was ACTUALLY TRANSMITTED, not the
#        pre-sanitize draft -- send markdown that the sanitizer rewrites
#        (**bold** -> <b>bold</b> HTML) and require the rendered HTML in the
#        record, never the raw asterisks. A recorder wired to the wrong
#        variable (e.g. $RAW_TEXT/$PROMPT) passes every other assertion here
#        since plain-text sends never change under sanitize -- only a body
#        that sanitize actually rewrites can catch that mistake. Still no
#        session-inbox file (DGN-1642). ---
BODY_CODE=200
OUT5="$(run_push --text "**bold**")"
assert_contains "markdown send succeeds" "$OUT5" "PUSH_RC=0"
LEDGER_LINE_5="$(tail -n1 "$LEDGER")"
assert_contains "ledger records the RENDERED html actually sent" "$LEDGER_LINE_5" "<b>bold</b>"
NEW_COUNT_5="$(new_outbound_files_since_baseline | grep -c . || true)"
assert_eq "still no session-inbox drop after a markdown send" "${NEW_COUNT_5:-0}" "0"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
