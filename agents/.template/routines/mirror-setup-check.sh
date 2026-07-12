#!/bin/bash
# mirror-setup-check.sh -- DGN-268 S4 preflight for the GCal/GTasks mirror.
#
# Verifies the three prerequisites the mirror needs to run and to connect a
# user's Google account (per-user OAuth, one login covering calendar + tasks +
# gmail.send). Prints a clear per-item OK / MISSING report and exits with a
# code the onboarding skill and the cron rail can branch on.
#
#   1. gws CLI present          (command -v gws)
#   2. gws authed + scopes      (gws auth status OK AND scopes include
#                                calendar, tasks, gmail.send)
#   3. cryptography importable  (python3 -c "import cryptography"; http_direct
#                                needs it for the If-Match write lane)
#
# Exit: 0 all-present / 1 one or more MISSING / 2 usage.
# --quiet suppresses the report (exit code only) -- the cron rail uses this for
# a silent fine-grained scope check on top of its own fast probe.
set -uo pipefail

QUIET=0
case "${1:-}" in
  --quiet) QUIET=1 ;;
  "" ) : ;;
  -h|--help) sed -n '2,16p' "$0"; exit 2 ;;
  *) echo "usage: mirror-setup-check.sh [--quiet]" >&2; exit 2 ;;
esac

_say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }

REQUIRED_SCOPES="calendar tasks gmail.send"
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

# 2) auth + scopes ---------------------------------------------------------
if [ "$have_gws" = "1" ]; then
  status_json="$(gws auth status 2>/dev/null || true)"
  if [ -z "$status_json" ] || ! printf '%s' "$status_json" | grep -q '"scopes"'; then
    _say "[MISSING] Google auth -- run: gws auth setup   then: gws auth login -s calendar,tasks,gmail.send"
    missing=1
  else
    # Extract granted scopes, match each required scope by substring (the
    # granted values are full URLs like .../auth/calendar; gmail.send is
    # .../auth/gmail.send). python3 keeps this JSON-safe and portable.
    granted="$(printf '%s' "$status_json" | python3 -c '
import json, sys
try:
    print("\n".join(json.load(sys.stdin).get("scopes", [])))
except Exception:
    pass' 2>/dev/null)"
    scope_missing=""
    for s in $REQUIRED_SCOPES; do
      if ! printf '%s\n' "$granted" | grep -q "/auth/${s}\b"; then
        scope_missing="$scope_missing $s"
      fi
    done
    if [ -n "$scope_missing" ]; then
      _say "[MISSING] Google scopes:${scope_missing} -- re-grant: gws auth login -s calendar,tasks,gmail.send"
      missing=1
    else
      _say "[OK]      Google auth + scopes (calendar, tasks, gmail.send)"
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
  _say "All mirror prerequisites present."
  exit 0
fi
_say ""
_say "One or more prerequisites are missing (see above)."
exit 1
