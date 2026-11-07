#!/bin/bash
# test-lane: fast
# test-steward-env.sh -- DGN-1811: the STEWARD_ENV_FILE contract.
#
#   A. parity: routines/lib/steward_env.sh steward_conf_value (push.sh's
#      reader) == routines/lib/conf_reader.py conf_get == bridge/
#      machine_gate.py steward_env_file, over quoted / spaced / commented /
#      empty / duplicated fixtures. The resolvers must never disagree.
#   B. steward_env_estate_default: marker -> steward .env; no marker, PoC,
#      self, missing root, missing .env -> "".
#   C. steward_env_stamp: writes only when no active line; an active line
#      (even empty) is kept; the stamped value round-trips through A.
#   D. source pins: push.sh reads STEWARD_ENV_FILE through the lib (not
#      read_kv); mint.sh calls the stamp; pack-lag's owner-copy BEHIND push
#      declares --audience owner.
# Sandbox only (mktemp); never touches a live instance or channel.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$(cd "$ROUTINES_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TEMPLATE_DIR/../.." && pwd)"
LIB="$ROUTINES_DIR/lib/steward_env.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL $1"; }
eq()  { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got [$2] want [$3])"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
unset DOGANY_MACHINE_STEWARD_FILE DOGANY_SHARED_HOME
export HOME="$TMP/home"
mkdir -p "$HOME"

# shellcheck source=../lib/steward_env.sh
. "$LIB"

py_values() { # <conf> -> "<conf_get>|<machine_gate or NONE>"
  python3 - "$ROUTINES_DIR/lib" "$TEMPLATE_DIR" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
from conf_reader import conf_get
from bridge.machine_gate import steward_env_file
v = conf_get("STEWARD_ENV_FILE", sys.argv[3])
r = steward_env_file(sys.argv[3])
print("%s|%s" % (v, r if r is not None else "NONE"))
PY
}

echo "A. resolver parity"
REAL="$TMP/op env/.env"
mkdir -p "$(dirname "$REAL")"; : > "$REAL"
REAL2="$TMP/op2/.env"
mkdir -p "$(dirname "$REAL2")"; : > "$REAL2"
n=0
check_parity() { # <label> <conf body>
  n=$((n + 1))
  local conf="$TMP/parity$n.conf" bashv pyv want_gate
  printf '%s' "$2" > "$conf"
  bashv="$(steward_conf_value "$conf" STEWARD_ENV_FILE)"
  pyv="$(py_values "$conf")"
  eq "A$n $1: bash == conf_get" "$bashv" "${pyv%%|*}"
  if [[ -n "$bashv" && -f "$bashv" ]]; then want_gate="$bashv"; else want_gate="NONE"; fi
  eq "A$n $1: bash resolvability == machine_gate" "$want_gate" "${pyv#*|}"
}
check_parity "plain"             "STEWARD_ENV_FILE=$REAL2"$'\n'
check_parity "double quoted"     "STEWARD_ENV_FILE=\"$REAL2\""$'\n'
check_parity "single quoted"     "STEWARD_ENV_FILE='$REAL2'"$'\n'
check_parity "quoted w/ space"   "STEWARD_ENV_FILE=\"$REAL\""$'\n'
check_parity "padding"           "   STEWARD_ENV_FILE=  $REAL2   "$'\n'
check_parity "commented only"    "# STEWARD_ENV_FILE=$REAL2"$'\n'
check_parity "empty"             "STEWARD_ENV_FILE="$'\n'
check_parity "first line wins"   "STEWARD_ENV_FILE=$TMP/nope.env"$'\n'"STEWARD_ENV_FILE=$REAL2"$'\n'
check_parity "no trailing nl"    "X=1"$'\n'"STEWARD_ENV_FILE=$REAL2"
check_parity "prefix key"        "STEWARD_ENV_FILE_OLD=$REAL2"$'\n'
check_parity "missing target"    "STEWARD_ENV_FILE=$TMP/nope.env"$'\n'
eq "A missing conf -> empty" "$(steward_conf_value "$TMP/absent.conf" STEWARD_ENV_FILE)" ""

echo "B. estate default (machine-steward marker)"
OP="$TMP/estate/op"; NEW="$TMP/estate/new"
mkdir -p "$OP/.telegram_bot" "$NEW/config"; : > "$OP/.telegram_bot/.env"
MARK="$HOME/.dogany/machine-steward"
eq "B1 no marker (plain user install) -> empty" "$(steward_env_estate_default "$NEW")" ""
mkdir -p "$HOME/.dogany"
printf '# designated steward\n\n%s\n' "$OP" > "$MARK"
OP_REAL="$(cd -P "$OP" && pwd)"
eq "B2 default marker path -> steward .env" "$(steward_env_estate_default "$NEW")" "$OP_REAL/.telegram_bot/.env"
eq "B3 the steward itself -> empty" "$(steward_env_estate_default "$OP")" ""
ALT="$TMP/alt-marker"; printf '%s\n' "$OP" > "$ALT"; rm -f "$MARK"
eq "B4 DOGANY_MACHINE_STEWARD_FILE honored" \
  "$(DOGANY_MACHINE_STEWARD_FILE="$ALT" steward_env_estate_default "$NEW")" "$OP_REAL/.telegram_bot/.env"
POC="$TMP/estate/poc/trial"; mkdir -p "$POC/.telegram_bot"; : > "$POC/.telegram_bot/.env"
printf '%s\n' "$POC" > "$MARK"
eq "B5 PoC marker -> empty" "$(steward_env_estate_default "$NEW")" ""
printf '%s\n' "$TMP/estate/gone" > "$MARK"
eq "B6 marker names a missing root -> empty" "$(steward_env_estate_default "$NEW")" ""
NOENV="$TMP/estate/noenv"; mkdir -p "$NOENV"; printf '%s\n' "$NOENV" > "$MARK"
eq "B7 steward without .env -> empty" "$(steward_env_estate_default "$NEW")" ""

echo "C. stamp"
printf '%s\n' "$OP" > "$MARK"
printf 'AGENT_LANG=en\n# STEWARD_ENV_FILE=\n' > "$NEW/config/agent.conf"
out="$(steward_env_stamp "$NEW")"; rc=$?
eq "C1 stamp rc" "$rc" "0"
eq "C1 stamped value resolves via the shared reader" \
  "$(steward_conf_value "$NEW/config/agent.conf" STEWARD_ENV_FILE)" "$OP_REAL/.telegram_bot/.env"
eq "C1 python reader agrees" "$(py_values "$NEW/config/agent.conf")" \
  "$OP_REAL/.telegram_bot/.env|$OP_REAL/.telegram_bot/.env"
before="$(cat "$NEW/config/agent.conf")"
out="$(steward_env_stamp "$NEW")"
eq "C2 re-stamp is idempotent" "$(cat "$NEW/config/agent.conf")" "$before"
case "$out" in *"already declared"*) ok "C2 says kept" ;; *) bad "C2 says kept: $out" ;; esac
printf 'STEWARD_ENV_FILE=\n' > "$NEW/config/agent.conf"
steward_env_stamp "$NEW" >/dev/null
eq "C3 explicit empty declaration kept" "$(cat "$NEW/config/agent.conf")" "STEWARD_ENV_FILE="
rm -f "$MARK"
printf 'AGENT_LANG=en\n' > "$NEW/config/agent.conf"
out="$(steward_env_stamp "$NEW")"
eq "C4 no operator -> conf untouched" "$(cat "$NEW/config/agent.conf")" "AGENT_LANG=en"
case "$out" in *"no operator"*) ok "C4 says why" ;; *) bad "C4 says why: $out" ;; esac
SP="$TMP/sp ace/op"; mkdir -p "$SP/.telegram_bot"; : > "$SP/.telegram_bot/.env"
printf '%s\n' "$SP" > "$MARK"
steward_env_stamp "$NEW" >/dev/null 2>&1
eq "C5 unsafe path (space) not written" "$(cat "$NEW/config/agent.conf")" "AGENT_LANG=en"
rm -rf "$NEW/config"
eq "C6 no conf -> rc 0" "$(steward_env_stamp "$NEW" >/dev/null; echo $?)" "0"

echo "D. source pins"
PUSH="$ROUTINES_DIR/push.sh"
if grep -q 'read_kv STEWARD_ENV_FILE' "$PUSH"; then bad "D1 push.sh still parses STEWARD_ENV_FILE with read_kv"; else ok "D1 push.sh has no read_kv STEWARD_ENV_FILE"; fi
if grep -q 'steward_conf_value "$_AGENT_CONF" STEWARD_ENV_FILE' "$PUSH"; then ok "D2 push.sh uses the shared reader"; else bad "D2 push.sh uses the shared reader"; fi
if [[ -f "$REPO_ROOT/scripts/mint.sh" ]]; then
  if grep -q 'steward_env_stamp "$PROJECT_ROOT"' "$REPO_ROOT/scripts/mint.sh"; then ok "D3 mint.sh stamps"; else bad "D3 mint.sh stamps"; fi
fi
PL="$ROUTINES_DIR/pack-lag.sh"
body="$(sed -n '/^_notify() {/,/^}/p' "$PL")"
case "$body" in *"--audience owner"*) ok "D4 pack-lag BEHIND push declares owner" ;; *) bad "D4 pack-lag BEHIND push declares owner" ;; esac
body="$(sed -n '/^_notify_operator() {/,/^}/p' "$PL")"
case "$body" in *"--audience operator --no-owner-fallback"*) ok "D5 evidence push stays operator-only" ;; *) bad "D5 evidence push stays operator-only" ;; esac

echo "test-steward-env: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
