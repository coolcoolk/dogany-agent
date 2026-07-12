#!/bin/bash
# mirror-setup-check.sh -- DGN-268 S4 preflight for the GCal/GTasks mirror.
#
# Verifies the prerequisites the mirror needs to run and to connect a user's
# Google account (per-user OAuth). Prints a clear per-item OK / MISSING report
# and exits with a code the onboarding skill and the cron rails branch on.
#
#   1. gws CLI present          (command -v gws)
#   2. gws authed + scopes      (gws auth status OK AND the REQUIRED scope set
#                                is granted -- default calendar+tasks+gmail.send)
#   3. cryptography importable  (python3 -c "import cryptography"; http_direct
#                                needs it for the If-Match write lane)
#
# Scope tiers (DGN-268 merge-gate FIX 2):
#   - ONBOARDING preflight + mailer need the FULL triple (calendar,tasks,
#     gmail.send) so email-send works.
#   - the CRON RAILS need only calendar,tasks -- a user without gmail.send must
#     still keep their calendar/tasks mirror RUNNING. The rails pass
#     `--require calendar,tasks`.
#
# Flags:
#   --quiet              exit code only (no report) -- the cron rails use this.
#   --require <list>     comma-separated scope subset to require
#                        (default: calendar,tasks,gmail.send). Names map to the
#                        canonical scope URLs below.
#
# Exit: 0 all-present / 1 one or more MISSING / 2 usage.
set -uo pipefail

QUIET=0
REQUIRE="calendar,tasks,gmail.send"
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) QUIET=1; shift ;;
    --require) REQUIRE="${2:-}"; shift 2 ;;
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
    tasks)      printf '%s' "https://www.googleapis.com/auth/tasks" ;;
    gmail.send) printf '%s' "https://www.googleapis.com/auth/gmail.send" ;;
    *)          printf '%s' "https://www.googleapis.com/auth/$1" ;;
  esac
}

# The exact grant command a user runs (their OWN auth -- RULES). `-s/--services`
# only limits the scope PICKER by service name and does NOT pin gmail.send, so
# we pass the exact scope URLs via `--scopes` (verified: gws auth --help,
# DGN-268 merge-gate FIX 3).
GRANT_CMD="gws auth login --scopes $(_scope_url calendar),$(_scope_url tasks),$(_scope_url gmail.send)"

missing=0

# 1) gws CLI ---------------------------------------------------------------
if command -v gws >/dev/null 2>&1; then
  _say "[OK]      gws CLI installed"
  have_gws=1
else
  _say "[MISSING] gws CLI -- install with: npm i -g @googleworkspace/gws"
  have_gws=0
  missing=1
fi

# 2) auth + required scopes ------------------------------------------------
if [ "$have_gws" = "1" ]; then
  status_json="$(gws auth status 2>/dev/null || true)"
  if [ -z "$status_json" ] || ! printf '%s' "$status_json" | grep -q '"scopes"'; then
    _say "[MISSING] Google auth -- run: gws auth setup   then: ${GRANT_CMD}"
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
      _say "[MISSING] Google scopes:${scope_missing} -- re-grant: ${GRANT_CMD}"
      missing=1
    else
      _say "[OK]      Google auth + scopes (${REQUIRE})"
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
