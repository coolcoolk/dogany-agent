#!/bin/bash
# test-lane: fast    # measured 1.9s (2026-09-18, DGN-1566 sandbox rewrite; was 1.5s in place)
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
# DGN-1566: THIS SUITE USED TO RUN IN PLACE. It drove push.sh straight out of
# the tree it lives in, and push.sh resolves everything it writes from its own
# location ($SCRIPT_DIR/../.telegram_bot) -- so every run appended real rows
# into the HOST tree's outbound-ledger.jsonl and its real
# push-guard-override.log, and mkdir'd files/program/.idrill-arm that the trap
# left behind. On the canonical repo the host tree IS agents/.template, and
# mint.sh copied the lot into every newborn (measured: a 481K guard log).
# Sandboxing here fixes that. DGN-1642: push.sh no longer drops a
# session-inbox file at all (leg 2 deleted at the producer) -- the ledger row
# is the only record now, and case 4 below asserts both: the ledger row
# exists under the sandbox, and no session-inbox drop exists anywhere.
#
# WHY NOT tests/lib/mint_fixture.sh (the REAL-mint fake-HOME sandbox the
# tests/ suites use): this file SHIPS TO INSTANCES. A minted instance carries
# routines/tests/ but neither scripts/mint.sh nor tests/lib/ (measured on a
# fresh mint), so a mint-fixture dependency would turn this suite red on every
# live agent's pre-push gate -- and skipping-when-absent would delete the
# cover exactly where the suite still has a real reader. The sandbox below
# needs only what both trees always have: routines/ and bridge/ side by side.
#
# HOW IT IS BUILT, and why it is not simply `ln -s .../routines "$INST/routines"`:
# push.sh writes to "$SCRIPT_DIR/../.telegram_bot", and `..` is resolved by
# the KERNEL, physically -- through a symlinked routines/ it lands back in the
# real parent, i.e. the host tree (measured: the drop reappeared in
# agents/.template). So the sandbox's routines/ and bridge/ are REAL dirs
# holding a symlink per entry: `$INST/routines/push.sh` is the byte-identical
# file under test, `$INST/routines/..` is `$INST`, and every write is
# sandboxed. Nothing is copied, so the cost stays in the fast lane.
#
# Run: bash routines/tests/test-push-idrill-exitcode.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_ROOT="$(cd "$ROUTINES_DIR/.." && pwd)"

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
trap 'rm -rf "$TMP"' EXIT

# TEST-ONLY- prefix = uniform fake-fixture form recognized by secret-sweep
# cat7 (DGN-1058); replaces the .sweepignore path allowlist for this file.
FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=TEST-ONLY-token123
ALLOWED_USER_IDS=12345
ENV_EOF

# DGN-1566 sandbox instance. One symlink per entry, so the sandbox dirs
# themselves are REAL and "$SCRIPT_DIR/.." lands inside the sandbox (see the
# header). __pycache__ is skipped on purpose: linking it would send the hop's
# bytecode writes back into the host tree, which is the leak in miniature.
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

# Instance layout push.sh expects relative to itself: ../bridge (python hop
# resolves bridge.artifacts/bridge.formatting) and ../files/program/.idrill-arm.
ARM_DIR="$INST/files/program/.idrill-arm"
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
  # DGN-1122: guard sees this tests/-path harness in ancestry; curl is
  # stubbed, so opt in explicitly (override receipt lands on stderr).
  PATH="$STUB:$PATH" BODY_CODE="$BODY_CODE" KB_CODE="$KB_CODE" \
    PUSH_GUARD_OVERRIDE=send-anyway \
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

# --- 4. DGN-1566: the sandbox actually holds. push.sh records every DELIVERED
#        send (cases 1 and 2; case 3's body never left) as a ledger row
#        (DGN-1642: no session-inbox drop -- the ledger row IS the record) --
#        and it must exist HERE, under the sandbox root. Asserted positively,
#        on the sandbox, rather than negatively on the host tree ("nothing new
#        appeared in the instance's ledger"): on a live agent the host ledger
#        is written and read by a running bridge, so a negative assertion
#        would race a real push and flake the push gate.
LEDGER_ROWS="$(wc -l < "$INST/.telegram_bot/outbound-ledger.jsonl" 2>/dev/null | tr -d ' ')"
if [[ "$LEDGER_ROWS" == "2" ]]; then
  ok "sandboxed: both delivered sends recorded under the sandbox, not the host tree"
else
  fail "sandboxed: expected 2 outbound-ledger rows under $INST, got $LEDGER_ROWS"
fi
DROPS="$(find "$INST/.telegram_bot/session-inbox" -name 'outbound-*.md' 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$DROPS" == "0" ]]; then
  ok "sandboxed: no session-inbox drops (DGN-1642: leg 2 deleted)"
else
  fail "sandboxed: expected 0 session-inbox drops under $INST, got $DROPS"
fi

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
