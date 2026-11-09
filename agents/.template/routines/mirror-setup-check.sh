#!/bin/bash

# gws PATH rescue (2026-08-22). DGN-963 reordered PATH and dropped
# $HOME/.npm-global/bin as "dead" -- but gws lives there and nothing else does.
# Every gate below is `command -v gws`, so the whole calendar sync went false
# and exited 0 SILENTLY: no error, no warning, just no sync, while blocks
# retired locally kept living in the owner Google Calendar. Same defect that
# killed route-find agent-browser the same day. Bind to known install dirs,
# not to a PATH policy this tool does not own.
for _gws_dir in "$HOME/.npm-global/bin" "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin; do
  if [ -x "$_gws_dir/gws" ]; then case ":$PATH:" in *":$_gws_dir:"*) :;; *) PATH="$PATH:$_gws_dir";; esac; fi
done
export PATH
# mirror-setup-check.sh -- calendar mirror preflight.
# Modern installs use framework Google consent; gws-cli is opt-in legacy.
# Framework connects need calendar; active kit mirrors also need tasks.
# Legacy full preflight retains calendar,tasks,gmail.send.
# --quiet: exit code only; --require: explicit comma-separated scope subset.
# Exit: 0 all-present / 1 missing / 2 usage.
set -uo pipefail

QUIET=0
REQUIRE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) QUIET=1; shift ;;
    --require)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "--require needs a comma-separated scope list" >&2; exit 2
      fi
      REQUIRE="$2"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 2 ;;
    *) echo "usage: mirror-setup-check.sh [--quiet] [--require calendar,tasks[,gmail.send]]" >&2; exit 2 ;;
  esac
done

_say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }

# Canonical OAuth scope URLs (what gws stores in `auth status`.scopes). Matching
# is exact-URL, not substring, so calendar does not false-match calendar.readonly.
_scope_url() {
  case "$1" in
    calendar)   printf '%s' "https://www.googleapis.com/auth/calendar" ;;
    gmail.send) printf '%s' "https://www.googleapis.com/auth/gmail.send" ;;
    *)          printf '%s' "https://www.googleapis.com/auth/$1" ;;
  esac
}

missing=0

# Resolve the same two kit-conf coordinates as the mirror routines. An absent
# key, auto, and compat all select the link flow; gws-cli is opt-in legacy.
_AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_CONF="$_AGENT_ROOT/config/lifekit.conf"
if [ -f "$_AGENT_ROOT/routines/lib/life-engine.sh" ]; then
  . "$_AGENT_ROOT/routines/lib/life-engine.sh"
  _LE_CONF="$(life_engine_conf_path "$_AGENT_ROOT")"
  [ -n "$_LE_CONF" ] && _CONF="$_LE_CONF"
elif [ -f "$_AGENT_ROOT/config/kit_life.conf" ]; then
  _CONF="$_AGENT_ROOT/config/kit_life.conf"
fi
_TRANSPORT="$( { grep '^CALENDAR_TRANSPORT=' "$_CONF" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"

# The installed kit still provisions a Tasks checklist (dec-663). Do not
# transfer that requirement to a kitless/off framework calendar connection.
if [ -z "$REQUIRE" ]; then
  if [ "$_TRANSPORT" = "gws-cli" ]; then
    REQUIRE="calendar,tasks,gmail.send"
  elif [ -d "$_AGENT_ROOT/mirror" ] && grep -Eq "^LIFEKIT=[\"']?on[\"']?[[:space:]]*$" "$_CONF" 2>/dev/null; then
    REQUIRE="calendar,tasks"
  else
    REQUIRE="calendar"
  fi
fi

if [ "$_TRANSPORT" != "gws-cli" ]; then
  status_json="$(python3 -m service.calendar status --instance "$_AGENT_ROOT" --json 2>/dev/null || true)"
  scope_missing="$(printf '%s' "$status_json" | python3 -c '
import json, sys
required = sys.argv[1].split(",")
try:
    status = json.load(sys.stdin)
    granted = set(status.get("granted") or [])
    missing = [s for s in required if s not in granted]
    if status.get("connected") is not True or status.get("class") or status.get("health"):
        missing = missing or required
except Exception:
    missing = required
print(",".join(missing))' "$REQUIRE" 2>/dev/null)" || scope_missing="$REQUIRE"
  if [ -z "$scope_missing" ]; then
    _say "[OK]      Google connected (${REQUIRE})"
  else
    _say "[MISSING] Google connection/scopes: ${scope_missing} -- agent: run python3 -m service.google connect start --origin \"<instance-dir-name>:<chat_id>\" --scopes ${REQUIRE} --json"
    missing=1
  fi
else
  # 1) gws CLI -------------------------------------------------------------
  if command -v gws >/dev/null 2>&1; then
    _say "[OK]      gws CLI installed"
    have_gws=1
  else
    _say "[MISSING] gws CLI (legacy transport) -- use the Google calendar connect skill"
    have_gws=0
    missing=1
  fi

  # 2) auth + required scopes ----------------------------------------------
  if [ "$have_gws" = "1" ]; then
    status_json="$(gws auth status 2>/dev/null || true)"
    if [ -z "$status_json" ] || ! printf '%s' "$status_json" | grep -q '"scopes"'; then
      _say "[MISSING] Google auth -- use the Google calendar connect skill"
      missing=1
    else
      granted="$(printf '%s' "$status_json" | python3 -c '
import json, sys
try:
    print("\n".join(json.load(sys.stdin).get("scopes", [])))
except Exception:
    pass' 2>/dev/null)"
      scope_missing=""
      IFS=','
      for s in $REQUIRE; do
        url="$(_scope_url "$s")"
        # exact-line match against the granted scope URLs
        if ! printf '%s\n' "$granted" | grep -qxF "$url"; then
          scope_missing="$scope_missing $s"
        fi
      done
      unset IFS
      if [ -n "$scope_missing" ]; then
        _say "[MISSING] Google scopes:${scope_missing} -- use the Google calendar connect skill"
        missing=1
      else
        _say "[OK]      Google auth + scopes (${REQUIRE})"
      fi
    fi
  fi
fi

# 3) cryptography ----------------------------------------------------------
if python3 -c "import cryptography" >/dev/null 2>&1; then
  _say "[OK]      python cryptography module"
else
  _say "[MISSING] python cryptography -- install with: python3 -m pip install cryptography"
  missing=1
fi

if [ "$missing" = "0" ]; then
  _say ""
  _say "All required mirror prerequisites present."
  exit 0
fi
_say ""
_say "One or more prerequisites are missing (see above)."
exit 1
