#!/bin/bash
# test-lane: fast
# test-push-identifier-guard.sh -- the send rail REFUSES internal identifiers.
#
# Companion to routines/tests/test-owner-notice-identifier-guard.py, which
# pins the detector as a pure function. This suite pins the part that only an
# end-to-end run can show: push.sh -- the last hop before the messaging API --
# actually blocks, with nothing leaving for the owner.
#
# The reported defect: a detached routine sent the owner
#   dispatch completed: <job-label> (runid dsp-20260911-110101-1006)
#
# Cases:
#   1. the reported string -> exit 4, NO send, evidence + fix line on stderr
#   2. an absolute path in the body -> exit 4, NO send
#   3. clean Korean copy -> sent normally (false-positive contract: this is
#      the case that decides whether the guard survives in the tree)
#   4. approved copy carrying a slash command and a Korean exit-code label
#      -> sent normally
#   5. the refusal offers no override knob (a hit is a caller defect)
#
# No real send: curl is stubbed and its argv captured.
#
# DGN-1568: THIS SUITE USED TO RUN IN PLACE, driving push.sh straight out of
# the tree it lives in -- push.sh resolves every write from its own location
# ($SCRIPT_DIR/../.telegram_bot), so the two successful sends below (cases 3
# and 4) dropped real outbound-records into the HOST tree (measured: 2
# drops/run). Same sandbox pattern as
# routines/tests/test-push-idrill-exitcode.sh (DGN-1566): see that file's
# header for the full rationale.
#
# Run: bash routines/tests/test-push-identifier-guard.sh
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
assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}

TMP="$(mktemp -d /tmp/push-ident-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# TEST-ONLY- prefix = the uniform fake-fixture form secret-sweep cat7 knows.
FAKE_ENV="$TMP/fake.env"
cat > "$FAKE_ENV" <<'ENV_EOF'
TELEGRAM_BOT_TOKEN=TEST-ONLY-token123
ALLOWED_USER_IDS=12345
ENV_EOF

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

run_push() {
  # DGN-1122: the sender-side test-context guard sees this tests/-path harness
  # in its ancestry and would refuse first. curl is stubbed, so opt in
  # explicitly -- that guard is not what this suite measures.
  : > "$CURL_ARGS_LOG"
  local _out _rc=0
  _out="$(PATH="$STUB:$PATH" PUSH_GUARD_OVERRIDE=send-anyway \
    bash "$PUSH_SH" --env "$FAKE_ENV" "$@" 2>&1)" || _rc=$?
  printf '%s\nPUSH_RC=%s\n' "$_out" "$_rc"
}

echo "== push.sh execution-identifier guard =="

# --- 1. the reported string ---
echo "--- 1: the reported dispatch notice is refused ---"
OUT="$(run_push --text "dispatch completed: callcrm-restructure-v2 (runid dsp-20260911-110101-1006)")"
assert_contains "1a: refused with exit 4" "$OUT" "PUSH_RC=4"
assert_contains "1b: refusal names the guard" "$OUT" "BLOCKED (execution-identifier guard)"
assert_contains "1c: refusal names the found shape" "$OUT" "found run-id: dsp-20260911-110101-1006"
assert_contains "1d: refusal tells the caller what to do" "$OUT" "fix: delete the identifier"
if [[ -s "$CURL_ARGS_LOG" ]]; then
  fail "1e: nothing was sent"
else
  ok "1e: nothing was sent"
fi

# --- 2. an absolute path ---
echo "--- 2: an absolute path in the body is refused ---"
OUT="$(run_push --text "점검 실패 -- 로그: /Users/someone/agent/.telegram_bot/x.stderr.log")"
assert_contains "2a: refused with exit 4" "$OUT" "PUSH_RC=4"
assert_contains "2b: refusal names the path shape" "$OUT" "found absolute-path:"
if [[ -s "$CURL_ARGS_LOG" ]]; then
  fail "2c: nothing was sent"
else
  ok "2c: nothing was sent"
fi

# --- 3. clean Korean copy still sends (false-positive contract) ---
echo "--- 3: clean owner copy is unaffected ---"
# Body is the owner-confirmed completion notice (2026-09-11), name included:
# the notice carries a readable job name on purpose, so the rail must be
# proven to pass a NAMED notice -- not only a nameless one.
OUT="$(run_push --text "✅ 작업완료: 고객 관계 정리 - 정리해서 보고드릴게요.")"
assert_contains "3a: exit 0" "$OUT" "PUSH_RC=0"
assert_not_contains "3b: guard stayed silent" "$OUT" "execution-identifier guard"
assert_contains "3c: body reached the API stub" "$(cat "$CURL_ARGS_LOG")" "작업완료: 고객 관계 정리"

# --- 4. approved copy shapes still send ---
echo "--- 4: approved copy shapes are not flagged ---"
OUT="$(run_push --text "주간 사용량 한도로 대기 중입니다.
또는 /usageretry com.example.agent.consolidate
상시 /health 에 표시됩니다. 종료코드: 1")"
assert_contains "4a: exit 0" "$OUT" "PUSH_RC=0"
assert_not_contains "4b: guard stayed silent" "$OUT" "BLOCKED (execution-identifier guard)"

# --- 5. no override knob exists ---
echo "--- 5: the guard has no escape hatch ---"
assert_not_contains "5a: no override env in push.sh" "$(cat "$PUSH_SH")" "IDENT_OVERRIDE"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
