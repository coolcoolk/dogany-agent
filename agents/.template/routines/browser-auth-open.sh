#!/bin/bash
# browser-auth-open.sh -- authenticated browser open wrapper (DGN-609 Layer B).
#
# PURPOSE: gate every authenticated browser-open call through:
#   1. Rung-0 deny-domain: known-API/MCP domains are hard-rejected (exit 1).
#   2. Keychain key load: AGENT_BROWSER_ENCRYPTION_KEY must load from macOS
#      keychain. Fail-closed: missing/unloadable key = hard reject, no plaintext.
#   3. Session open: agent-browser with --session, --restore, --restore-check-url
#      or --restore-check-text. Restore-check failure signals re-login needed.
#
# Layer A env (AGENT_BROWSER_NAMESPACE, AGENT_BROWSER_IDLE_TIMEOUT_MS) is already
# bound via launchd plist and does not need to be set here.
#
# USAGE:
#   browser-auth-open.sh <site-class> <url> [--check-url <glob>] [--check-text <txt>]
#
# EXIT CODES:
#   0  opened successfully with a valid restored session
#   1  hard reject (domain policy, keychain failure, or bad args)
#   2  restore-check failed -- auth expired; caller must push re-login notice

set -euo pipefail

PROG="browser-auth-open"

# --------------------------------------------------------------------------
# Rung-0 deny-domain list (spec 1, DGN-609).
# Domains that have estate-connected API/MCP: browser path is forbidden.
# --------------------------------------------------------------------------
DENY_DOMAINS="notion.so notion.site slack.com google.com gmail.com calendar.google.com drive.google.com"

# --------------------------------------------------------------------------
# Usage.
# --------------------------------------------------------------------------
usage() {
    echo "Usage: $PROG <site-class> <url> [--check-url <glob>] [--check-text <txt>]" >&2
    echo "  site-class  isolation key (e.g. myapp, corp-wiki); combined with namespace for scoping" >&2
    echo "  url         target URL to open" >&2
    echo "  --check-url pattern that the post-restore URL must match (recommended)" >&2
    echo "  --check-text text that must be visible after restore (alternative to --check-url)" >&2
    exit 1
}

# --------------------------------------------------------------------------
# Argument parsing.
# --------------------------------------------------------------------------
if [ $# -lt 2 ]; then
    usage
fi

SITE_CLASS="$1"
TARGET_URL="$2"
shift 2

CHECK_URL=""
CHECK_TEXT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check-url)
            [ $# -ge 2 ] || { echo "$PROG: --check-url requires a value" >&2; exit 1; }
            CHECK_URL="$2"; shift 2 ;;
        --check-text)
            [ $# -ge 2 ] || { echo "$PROG: --check-text requires a value" >&2; exit 1; }
            CHECK_TEXT="$2"; shift 2 ;;
        *) echo "$PROG: unknown option: $1" >&2; usage ;;
    esac
done

if [ -z "$CHECK_URL" ] && [ -z "$CHECK_TEXT" ]; then
    echo "$PROG: ERROR: at least one of --check-url or --check-text is required (spec 3.6)" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Rung-0: deny-domain hard reject.
# --------------------------------------------------------------------------
# Reject non-http(s) schemes (no file://, javascript:, data:, etc.).
case "$TARGET_URL" in
    http://*|https://*) : ;;
    *) echo "$PROG: ERROR: only http(s) URLs are permitted: $TARGET_URL" >&2; exit 1 ;;
esac

# Extract + normalize the host: strip scheme, then path/query/fragment, then
# userinfo (user:pass@), then port; strip a trailing FQDN dot; lowercase.
# This closes rung-0 bypass via case (NOTION.SO), trailing dot (notion.so.),
# and userinfo (evil.com@notion.so) -- all of which still resolve to the host.
URL_HOST="$(printf '%s' "$TARGET_URL" \
    | sed -E 's|^[a-zA-Z][a-zA-Z0-9+.-]*://||' \
    | sed -E 's|[/?#].*$||' \
    | sed -E 's|^[^@]*@||' \
    | sed -E 's|:.*$||' \
    | sed -E 's|\.$||' \
    | tr 'A-Z' 'a-z')"

for deny in $DENY_DOMAINS; do
    # Match exact host or any subdomain (e.g. api.notion.so -> notion.so).
    case "$URL_HOST" in
        "$deny"|*".$deny")
            echo "$PROG: DENIED -- $URL_HOST is covered by a native API or estate MCP." >&2
            echo "$PROG: Use the native API/MCP instead of the browser path. (spec rung-0, DGN-609)" >&2
            exit 1
            ;;
    esac
done

# --------------------------------------------------------------------------
# Keychain: load AGENT_BROWSER_ENCRYPTION_KEY (fail-closed, spec 3.3).
# Keychain item: generic-password, service=agent-browser-enc-key, account=<NAMESPACE>.
# --------------------------------------------------------------------------
NAMESPACE="${AGENT_BROWSER_NAMESPACE:-}"
if [ -z "$NAMESPACE" ]; then
    echo "$PROG: ERROR: AGENT_BROWSER_NAMESPACE is not set. Layer A env must be wired." >&2
    exit 1
fi

# Belt: Layer A normally binds AGENT_BROWSER_IDLE_TIMEOUT_MS via the plist, but a
# cron/routine plist may carry NAMESPACE and forget it. Default it here so an
# auth session on the unattended host never opens without an auto-shutdown.
if [ -z "${AGENT_BROWSER_IDLE_TIMEOUT_MS:-}" ]; then
    export AGENT_BROWSER_IDLE_TIMEOUT_MS="300000"
    echo "$PROG: WARN: AGENT_BROWSER_IDLE_TIMEOUT_MS unset; defaulting to 300000ms." >&2
fi

KEYCHAIN_KEY=""
if command -v security >/dev/null 2>&1; then
    KEYCHAIN_KEY="$(security find-generic-password \
        -s "agent-browser-enc-key" \
        -a "$NAMESPACE" \
        -w 2>/dev/null || true)"
fi

if [ -z "$KEYCHAIN_KEY" ]; then
    echo "$PROG: ERROR: AGENT_BROWSER_ENCRYPTION_KEY could not be loaded from keychain." >&2
    echo "$PROG: Service=agent-browser-enc-key account=$NAMESPACE must exist in macOS keychain." >&2
    echo "$PROG: Refusing to open -- plaintext session state is not permitted. (spec 3.3, DGN-609)" >&2
    exit 1
fi

export AGENT_BROWSER_ENCRYPTION_KEY="$KEYCHAIN_KEY"

# --------------------------------------------------------------------------
# Build restore-check flags as a quoted array (no word-split / glob expansion).
# A bare string here lets values like '*' glob against cwd and spaces smuggle
# extra flags into agent-browser -- both are security holes in the open call.
# --------------------------------------------------------------------------
CHECK_ARGS=()
if [ -n "$CHECK_URL" ]; then
    CHECK_ARGS=(--restore-check-url "$CHECK_URL")
elif [ -n "$CHECK_TEXT" ]; then
    CHECK_ARGS=(--restore-check-text "$CHECK_TEXT")
fi

# --------------------------------------------------------------------------
# Open with restore (spec 3.1 + 3.6).
# --restore-save auto: failed restore does not overwrite known-good state.
# --------------------------------------------------------------------------
if ! agent-browser \
        --session "$SITE_CLASS" \
        --restore \
        --restore-save auto \
        "${CHECK_ARGS[@]}" \
        open "$TARGET_URL"; then
    echo "$PROG: restore-check FAILED for $SITE_CLASS / $TARGET_URL" >&2
    echo "$PROG: Auth may be expired. Caller should push re-login notice + open ticket. (spec 3.6)" >&2
    exit 2
fi
