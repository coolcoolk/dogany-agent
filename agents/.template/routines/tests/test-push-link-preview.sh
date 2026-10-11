#!/bin/bash
# test-lane: fast
# test-push-link-preview.sh -- DGN-1964 push.sh link previews default OFF.
#
# No real Telegram send: curl is stubbed, every call's argv captured. Each
# text send path must carry link_preview_options={"is_disabled":true}:
#   1. plain send (HTML leg)            2. HTML 400 -> plain retry (both legs)
#   3. --strict send                    4. --strict --edit (editMessageText)
#   5. idrill keyboard message          6. --link-preview opt-in omits the field
#   7. --capabilities lists no-preview
# Sandbox instance per routines/tests/test-push-idrill-exitcode.sh (DGN-1566).
#
# Run: bash routines/tests/test-push-link-preview.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_ROOT="$(cd "$ROUTINES_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
eq()   { if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 -- got '$2' want '$3'"; fi; }

TMP="$(mktemp -d /tmp/push-preview-test.XXXXXX)"
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

ARM_DIR="$INST/files/program/.idrill-arm"
mkdir -p "$ARM_DIR"
ARM_ID="d1964001"
cat > "$ARM_DIR/$ARM_ID" <<'ARM_EOF'
{
  "session_id": "sess-preview-test",
  "cmd": ["/bin/true"],
  "step_final": "1",
  "step_buttons": {"1": [["8", "8"]]},
  "step_text": {"1": "reps?"}
}
ARM_EOF

# One file per curl call (call.N); STUB_CODES="400 200" answers in order
# (missing entries -> 200).
CALLS="$TMP/calls"
STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/curl" <<CURL_EOF
#!/bin/bash
n=\$(ls "$CALLS" 2>/dev/null | wc -l | tr -d ' ')
printf '%s\n' "\$@" > "$CALLS/call.\$n"
OUT=""; ARGS=("\$@")
for ((i=0;i<\${#ARGS[@]};i++)); do [[ "\${ARGS[\$i]}" == "-o" ]] && OUT="\${ARGS[\$((i+1))]}"; done
read -r -a codes <<< "\${STUB_CODES:-}"
code="\${codes[\$n]:-200}"
if [[ "\$code" == "200" ]]; then
  printf '%s' '{"ok":true,"result":{"message_id":77}}' > "\$OUT"
else
  printf '%s' "{\"ok\":false,\"error_code\":\$code,\"description\":\"Bad Request: can't parse entities\"}" > "\$OUT"
fi
printf '%s' "\$code"
CURL_EOF
chmod +x "$STUB/curl"

run_push() { # run_push <codes> <push args...>; sets RC
  rm -rf "$CALLS"; mkdir -p "$CALLS"
  local codes="$1"; shift
  STUB_CODES="$codes" PATH="$STUB:$PATH" PUSH_GUARD_OVERRIDE=send-anyway \
    bash "$PUSH_SH" --env "$FAKE_ENV" "$@" >"$TMP/out" 2>"$TMP/err"; RC=$?
}
ncalls() { ls "$CALLS" | wc -l | tr -d ' '; }
OFF='link_preview_options={"is_disabled":true}'
all_off() { # all_off <desc>: every captured call carries the OFF field
  local f bad=0
  for f in "$CALLS"/call.*; do grep -qxF "$OFF" "$f" || bad=1; done
  [[ "$(ncalls)" -gt 0 && "$bad" -eq 0 ]] && ok "$1" || fail "$1 -- $(cat "$CALLS"/call.* 2>/dev/null | tr '\n' ' ')"
}

PLACE='내일 <a href="https://naver.me/xYz123">성산일출봉</a> 09:00 출발'

echo "== DGN-1964 push.sh link previews default off =="

run_push "" --audience owner --text "$PLACE"
eq "plain: rc 0" "$RC" 0
eq "plain: one call" "$(ncalls)" 1
all_off "plain: preview off"

run_push "400 200" --audience owner --text "$PLACE"
eq "html-400 retry: rc 0" "$RC" 0
eq "html-400 retry: two calls" "$(ncalls)" 2
all_off "html-400 retry: preview off on both legs"

run_push "" --audience owner --strict --chat 555 --text "travel alert https://naver.me/xYz123"
eq "strict: rc 0" "$RC" 0
all_off "strict: preview off"

run_push "" --audience owner --strict --edit 77 --chat 555 --text "travel alert v2 https://naver.me/xYz123"
eq "edit: rc 0" "$RC" 0
grep -q '/editMessageText' "$CALLS/call.0" && ok "edit: editMessageText endpoint" || fail "edit: endpoint"
all_off "edit: preview off"

run_push "" --audience owner --text "본문
[[IDRILL:$ARM_ID]]"
eq "idrill: rc 0" "$RC" 0
eq "idrill: body + keyboard calls" "$(ncalls)" 2
all_off "idrill: preview off on body and keyboard"

run_push "" --audience owner --link-preview --text "$PLACE"
eq "opt-in: rc 0" "$RC" 0
if grep -q '^link_preview_options=' "$CALLS"/call.*; then fail "opt-in: field must be omitted"; else ok "opt-in: field omitted (Telegram default preview)"; fi

CAP="$(bash "$PUSH_SH" --capabilities 2>&1)"
[[ "$CAP" =~ (^|[[:space:]])no-preview($|[[:space:]]) ]] && ok "capabilities lists no-preview" || fail "capabilities: $CAP"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
