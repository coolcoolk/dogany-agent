#!/bin/bash
# dgn142_paste_test.sh -- DGN-142 multiline-paste regression (install wizard).
#
# Contract under test (DGN-142, drain shipped in the v1.0.2 wizard hotfix
# bundle; this scripted case is the regression battery the ticket asked for):
#   ask() must absorb an ENTIRE multi-line paste (full BotFather / userinfobot
#   message) into the answered value BEFORE extraction or any confirm runs --
#   leftover buffered lines must never leak into the next prompt one-per-read
#   (the original failure ate the y/n confirm and hit the 5-try abort cap).
#
# Method: source install.sh as a library (DOGANY_INSTALL_LIB=1) and drive
# ask() + extract_token / extract_user_id with a piped multi-line blob, the
# way a terminal paste lands (all lines buffered at once).
#
# SAFETY: library mode only -- no wizard step runs, nothing is written, no
# network, no token is real.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

say() { printf '%s\n' "$*"; }
ok()  { PASS=$((PASS+1)); say "  ok: $*"; }
bad() { FAIL=$((FAIL+1)); say "  FAIL: $*"; }

FAKE_TOKEN="1234567890:AAFakePasteTokenDGN142AAAAAAAAAAAAA"

# T1: full BotFather message paste -> token extracted, whole blob absorbed.
say "[T1] BotFather multi-line paste absorbed by one ask()"
OUT="$(
  printf 'Done! Congratulations on your new bot.\n\nUse this token to access the HTTP API:\n%s\n\nKeep your token secure and store it safely.\n' "$FAKE_TOKEN" \
  | bash -c '
      set -u
      export DOGANY_INSTALL_LIB=1
      set --
      # shellcheck disable=SC1091
      source "'"$SANDBOX"'/install.sh" >/dev/null 2>&1
      DRY_RUN=0; DOGANY_LANG=en
      blob=""
      ask blob "t: " "t: " >/dev/null 2>&1
      printf "TOKEN=%s\n" "$(extract_token "$blob")"
      printf "LINES=%s\n" "$(printf "%s\n" "$blob" | wc -l | tr -d " ")"
    '
)"
case "$OUT" in
  *"TOKEN=$FAKE_TOKEN"*) ok "token extracted from mid-paste line" ;;
  *) bad "token not extracted (got: $OUT)" ;;
esac
T1_LINES="$(printf '%s\n' "$OUT" | grep '^LINES=' | cut -d= -f2)"
if [ "${T1_LINES:-0}" -gt 1 ]; then
  ok "paste fully drained into the blob ($T1_LINES lines, none left for the next prompt)"
else
  bad "blob holds only line 1 -- drain regressed (lines=$T1_LINES)"
fi

# T2: userinfobot multi-line paste -> owner id extracted the same way.
say "[T2] userinfobot multi-line paste absorbed by one ask()"
OUT2="$(
  printf '@cooluser\nId: 11111111\nFirst: Cool\nLang: ko\n' \
  | bash -c '
      set -u
      export DOGANY_INSTALL_LIB=1
      set --
      # shellcheck disable=SC1091
      source "'"$SANDBOX"'/install.sh" >/dev/null 2>&1
      DRY_RUN=0; DOGANY_LANG=en
      blob=""
      ask blob "t: " "t: " >/dev/null 2>&1
      printf "ID=%s\n" "$(extract_user_id "$blob")"
    '
)"
case "$OUT2" in
  *"ID=11111111"*) ok "owner id extracted from mid-paste line" ;;
  *) bad "owner id not extracted (got: $OUT2)" ;;
esac

say ""
say "dgn142_paste_test: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
