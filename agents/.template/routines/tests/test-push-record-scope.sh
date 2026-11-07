#!/bin/bash
# test-lane: fast
# test-covers: *    # tree scanner, measured 1.50s (DGN-1734): runs on every push
# test-push-record-scope.sh -- DGN-1642 push.sh never drops a session-inbox
# turn on its own outbound record; DGN-1623's --record-ledger-only stays
# accepted as a no-op.
#
# DGN-1642: the DGN-1528 outbound-record contract fires ONE leg on every
# send now: the durable ledger row. The session-inbox drop (the old leg 2,
# which handed the sending session a free follow-up turn for its own
# record) is deleted at the producer -- push.sh never creates that turn,
# with or without --record-ledger-only. The flag is kept accepted, as a
# no-op, only so existing callers (scripts/pack/mint_run.sh) do not break.
#
# Tests:
#   1. default send (no flag) -> ledger gains 1 row AND session-inbox stays
#      EMPTY (pre-DGN-1642 this asserted the opposite: exactly 1
#      outbound-*.md file -- that assertion is gone; a real report-bearing
#      turn is proven separately, not by this record-only lane).
#   2. --record-ledger-only send -> ledger gains 1 row AND session-inbox
#      stays empty AND exit 0 (flag-compat: same observable outcome as the
#      default now, proving the flag is a harmless no-op, not that it
#      breaks).
#
# Pre-DGN-1642 failure mode (this suite's RED-before-fix): test 1's
# session-inbox-empty assertion fails on today's push.sh, which still
# writes the outbound-*.md drop at the old leg 2.
#
# DGN-1568 sandbox pattern (see test-dgn1528-outbound-record.sh header for
# full rationale): push.sh derives its ledger/inbox path from ITS OWN
# location ($SCRIPT_DIR/../.telegram_bot), so this suite runs push.sh out of
# a symlinked sandbox instance, never the host tree.
#
# Run: bash routines/tests/test-push-record-scope.sh
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

TMP="$(mktemp -d /tmp/push-record-scope-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# DGN-1568 sandbox instance (DGN-1566 pattern). One symlink per entry, so the
# sandbox dirs themselves are REAL and "$SCRIPT_DIR/.." lands inside the
# sandbox, not the host tree.
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

INSTANCE_DIR="$INST/.telegram_bot"
LEDGER="$INSTANCE_DIR/outbound-ledger.jsonl"
INBOX="$INSTANCE_DIR/session-inbox"

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
_ofile=""
prev=""
for a in "$@"; do
  if [[ "$prev" == "-o" ]]; then _ofile="$a"; fi
  prev="$a"
done
[[ -n "$_ofile" ]] && echo '{"ok":true}' > "$_ofile"
echo "200"
CURL_EOF
chmod +x "$STUB/curl"

run_push() {
  # DGN-1122: push.sh's sender-side guard detects this tests/-path harness in
  # its process ancestry and would refuse to send. curl is stubbed here, so
  # opt in EXPLICITLY (guard logs an override receipt on stderr).
  PATH="$STUB:$PATH" PUSH_GUARD_OVERRIDE=send-anyway \
    bash "$PUSH_SH" --env "$FAKE_ENV" "$@" 2>&1
  echo "PUSH_RC=$?"
}

echo "== DGN-1642/DGN-1623 push.sh outbound-record scope =="

# --- 1. default send (no flag) -> ledger row appended, session-inbox
#        stays EMPTY (DGN-1642: leg 2 -- the session-inbox drop -- is
#        deleted at the producer; this is no longer conditional on the
#        flag) ---
OUT1="$(run_push --text "default path message")"
assert_contains "default: push succeeds" "$OUT1" "PUSH_RC=0"
assert_eq "default: ledger has 1 row" \
  "$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')" "1"
INBOX_COUNT_1="$(find "$INBOX" -maxdepth 1 -type f -name 'outbound-*.md' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "default: session-inbox has 0 files (DGN-1642: no turn created)" "$INBOX_COUNT_1" "0"

# --- 2. --record-ledger-only send -> same observable outcome (flag-compat
#        no-op): ledger row appended, no inbox drop ---
OUT2="$(run_push --text "ledger-only message" --record-ledger-only)"
assert_contains "ledger-only: push succeeds" "$OUT2" "PUSH_RC=0"
assert_eq "ledger-only: ledger grows to 2 rows" \
  "$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')" "2"
LEDGER_LINE_2="$(tail -n1 "$LEDGER")"
assert_contains "ledger-only: ledger row carries the sent body" "$LEDGER_LINE_2" "ledger-only message"
INBOX_COUNT_2="$(find "$INBOX" -maxdepth 1 -type f -name 'outbound-*.md' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "ledger-only: session-inbox stays at 0 files (flag is a no-op)" "$INBOX_COUNT_2" "0"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
