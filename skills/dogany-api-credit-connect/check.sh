#!/usr/bin/env bash
# check.sh -- rule-lane carrier for dogany-api-credit-connect (lanes: in
# product.yaml). Stopgap until the chat secret intake (v2.7, dec-347).
#
#   check.sh present   s5a verify: is the API credit key stored where the
#                      dispatcher reads it? Never reads the secret on macOS
#                      (keychain attributes only); off macOS checks the file.
#   check.sh probe     s5b verify: one tiny call (probe.py: model list + a
#                      1-token message) with the key from the dispatcher's
#                      own api_key_read, so key path and credit are proven.
#   check.sh offer-due s0 value: "due" once per instance when PLAN is a Max
#                      or Team plan and no key is stored; records the offer
#                      in the same call, so a second call says not_due.
#
# All three reuse the dispatcher's functions verbatim (extracted from
# routines/dispatch-detached.sh), so this skill can never drift from the
# path the dispatcher really uses. The key is never printed, logged, put in
# argv or written anywhere.
#
# Output: one line "<verdict> <detail>" for the agent to translate.
#   present: present | missing | env_unset (off macOS: file var not set)
#   probe:   ok | missing | env_unset | key_rejected | credit_low | failed
#   offer-due: due | not_due <why>
# ROOT = instance root: $DOGANY_ROOT, else cwd. Exit 0 ok / 1 finding / 2 usage.
set -uo pipefail

ROOT="${DOGANY_ROOT:-$PWD}"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")")" && pwd)"

dispatcher() {  # instance copy first, then walk up from here
  local d="$HERE"
  [ -f "$ROOT/routines/dispatch-detached.sh" ] && { echo "$ROOT/routines/dispatch-detached.sh"; return 0; }
  while [ "$d" != "/" ]; do
    [ -f "$d/routines/dispatch-detached.sh" ] && { echo "$d/routines/dispatch-detached.sh"; return 0; }
    d="$(dirname "$d")"
  done
  echo "check.sh: routines/dispatch-detached.sh not found (set DOGANY_ROOT)" >&2
  return 2
}

# Load API_CREDIT_KEYCHAIN_* + api_key_present + api_key_read from the
# dispatcher. Fail closed when any piece is missing.
load_dispatcher_fns() {
  local src defs
  src="$(dispatcher)" || return 2
  defs="$(awk '
    /^API_CREDIT_KEYCHAIN_(SERVICE|ACCOUNT)=/ { print; next }
    /^api_key_(present|read)\(\) \{/ { grab=1 }
    grab { print }
    grab && /^\}/ { grab=0 }' "$src")"
  eval "$defs"
  if [ -z "${API_CREDIT_KEYCHAIN_SERVICE:-}" ] || ! declare -F api_key_present >/dev/null \
     || ! declare -F api_key_read >/dev/null; then
    echo "check.sh: key functions not found in $src" >&2
    return 2
  fi
}

# Off macOS the dispatcher reads ANTHROPIC_API_KEY_FILE from its env, which
# the bridge loads from <instance>/.telegram_bot/.env. Mirror that one hop.
load_key_file_var() {
  [ "$(uname -s)" = Darwin ] && return 0
  [ -n "${ANTHROPIC_API_KEY_FILE:-}" ] && return 0
  local envf="$ROOT/.telegram_bot/.env" v=""
  [ -r "$envf" ] && v="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}ANTHROPIC_API_KEY_FILE=//p' "$envf" | tail -n 1)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  v="${v/#\~/$HOME}"
  [ -n "$v" ] && export ANTHROPIC_API_KEY_FILE="$v"
  return 0
}

present_verdict() {
  if api_key_present; then
    echo "present"
    return 0
  fi
  if [ "$(uname -s)" != Darwin ] && [ -z "${ANTHROPIC_API_KEY_FILE:-}" ]; then
    echo "env_unset ANTHROPIC_API_KEY_FILE is not set (env or .telegram_bot/.env)"
  else
    echo "missing"
  fi
  return 1
}

cmd="${1:-}"
case "$cmd" in
  present)
    load_dispatcher_fns || exit 2
    load_key_file_var
    present_verdict
    exit $?
    ;;
  probe)
    load_dispatcher_fns || exit 2
    load_key_file_var
    present_verdict >/dev/null || { present_verdict; exit 1; }
    # Key: the dispatcher's own api_key_read, piped to stdin -- never argv.
    out="$( { api_key_read || true; echo; } | python3 -B "$HERE/probe.py" 2>/dev/null )"
    [ -n "$out" ] || out="failed probe produced no verdict"
    echo "$out"
    [ "$out" = ok ] && exit 0
    exit 1
    ;;
  offer-due)
    load_dispatcher_fns || exit 2
    load_key_file_var
    mark="$ROOT/.claude/.dogany-api-credit-offered"
    [ -e "$mark" ] && { echo "not_due already offered"; exit 0; }
    plan="${PLAN:-}"
    [ -n "$plan" ] || plan="$(sed -n 's/^[[:space:]]*PLAN=//p' "$ROOT/config/agent.conf" 2>/dev/null | tail -n 1 | tr -d '[:space:]\"'"'"'')"
    case "$plan" in
      max_5x|max_20x|team_standard|team_premium) ;;
      *) echo "not_due plan '${plan:-unset}' carries no API credit"; exit 0 ;;
    esac
    api_key_present && { echo "not_due key already stored"; exit 0; }
    mkdir -p "$ROOT/.claude" && date -u +%Y-%m-%dT%H:%M:%SZ > "$mark"
    echo "due"
    exit 0
    ;;
  *)
    sed -n '5,12p' "$0"
    exit 2
    ;;
esac
