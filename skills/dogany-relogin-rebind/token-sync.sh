#!/usr/bin/env bash
# token-sync.sh -- READ-ONLY credential store inspector for
# ~/.claude/.credentials.json vs macOS Keychain "Claude Code-credentials".
# dogany-relogin-rebind framework skill (DGN-393/394, promoted DGN-591).
#
# DGN-1050 RETIREMENT NOTICE (2026-08-23):
#   The `sync` subcommand (unconditional file -> keychain overwrite) is
#   RETIRED and hard-fails. Root cause of the estate-wide daily auth deaths:
#   the Claude CLI keeps ONE authoritative credential copy (keychain primary,
#   file fallback only) and does NOT rewrite the file after a runtime token
#   rotation -- so the file is stale BY DESIGN. Overwriting the keychain from
#   the file re-injects a superseded refresh token; its reuse triggers
#   server-side token-family revocation and kills every instance sharing the
#   account. The ONLY safe credential writer is the CLI itself:
#       claude auth login        (plain terminal, no env vars)
#   This script performs ZERO keychain writes. There is intentionally no
#   `security add-generic-password` call anywhere in this file.
#
# PLATFORM: macOS only. Claude CLI reads the login keychain first on Darwin;
# this skill has no counterpart on Linux (no keychain entry to drift), so it
# exits as not-applicable there without touching anything.
#
# Subcommands:
#   status   read-only report. Prints FILE/KEYCHAIN token hashes and a verdict:
#              MATCH    -- both stores hold the same tokens
#              DIVERGED -- stores differ. This is the NORMAL steady state after
#                          a runtime token rotation (file stale by design);
#                          it is NOT an error and requires NO action.
#            exit 0 for BOTH verdicts (divergence is not a failure signal),
#            exit 2 = real error (store unreadable), exit 3 = non-macOS.
#            (DGN-1050: the old 1=MISMATCH exit code was misinformation --
#            it flagged the designed steady state as a problem and prompted
#            the poisonous sync. Do not reintroduce it.)
#   sync     RETIRED (DGN-1050). Hard-fails with exit 2. See notice above.

set -euo pipefail

# -- platform guard (must precede any keychain/security call) --
# On non-Darwin hosts there is no login keychain to inspect; report clearly
# and exit 3 (not-applicable) WITHOUT running any keychain command.
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "NOT-APPLICABLE: dogany-relogin-rebind is macOS-only (Keychain inspection);" \
         "current platform is $(uname -s). No action taken." >&2
    exit 3
fi

CREDS_FILE="${HOME}/.claude/.credentials.json"
KEYCHAIN_SERVICE="Claude Code-credentials"

# -- helpers --

die_error() {
    echo "ERROR: $*" >&2
    exit 2
}

# Read accessToken from a JSON string (via python3)
extract_token() {
    local json="$1"
    python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
# token lives at .claudeAiOauth.accessToken
try:
    tok = data['claudeAiOauth']['accessToken']
    print(tok)
except (KeyError, TypeError):
    sys.exit(1)
" <<< "$json" || return 1
}

# SHA256 of a string, first 16 hex chars
short_hash() {
    local val="$1"
    printf '%s' "$val" | shasum -a 256 | cut -c1-16
}

# -- read sources --

read_file_token() {
    if [[ ! -f "$CREDS_FILE" ]]; then
        die_error "credentials file not found: $CREDS_FILE"
    fi
    local json
    json=$(cat "$CREDS_FILE") || die_error "cannot read $CREDS_FILE"
    local tok
    if ! tok=$(extract_token "$json"); then
        die_error "cannot parse .claudeAiOauth.accessToken from credentials file"
    fi
    echo "$tok"
}

read_file_json() {
    if [[ ! -f "$CREDS_FILE" ]]; then
        die_error "credentials file not found: $CREDS_FILE"
    fi
    cat "$CREDS_FILE" || die_error "cannot read $CREDS_FILE"
}

read_keychain_token() {
    local kc_json
    if ! kc_json=$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null); then
        die_error "keychain entry not found: $KEYCHAIN_SERVICE"
    fi
    local tok
    if ! tok=$(extract_token "$kc_json"); then
        die_error "cannot parse .claudeAiOauth.accessToken from keychain entry"
    fi
    echo "$tok"
}

# -- subcommands --

cmd_status() {
    # print file mtime
    local mtime
    if [[ -f "$CREDS_FILE" ]]; then
        mtime=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" "$CREDS_FILE" 2>/dev/null || echo "unknown")
    else
        mtime="FILE NOT FOUND"
    fi
    echo "credentials file mtime: $mtime"

    local file_json keychain_json
    file_json=$(read_file_json)
    keychain_json=$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null) \
        || die_error "keychain entry not found: $KEYCHAIN_SERVICE"

    # Display token hashes for visibility
    local file_tok keychain_tok
    file_tok=$(read_file_token) || die_error "cannot read file token"
    keychain_tok=$(read_keychain_token) || die_error "cannot read keychain token"

    local file_hash keychain_hash
    file_hash=$(short_hash "$file_tok")
    keychain_hash=$(short_hash "$keychain_tok")

    echo "FILE    token hash: ${file_hash}"
    echo "KEYCHAIN token hash: ${keychain_hash}"

    # Compare values: parse both JSONs and check accessToken and refreshToken fields.
    # Falls back to raw JSON comparison if either JSON fails to parse.
    local result
    result=$({
        echo "$file_json"
        echo "$keychain_json"
    } | python3 -c "
import json, sys

def extract_tokens(json_str):
    try:
        data = json.loads(json_str)
        return {
            'accessToken': data.get('claudeAiOauth', {}).get('accessToken'),
            'refreshToken': data.get('claudeAiOauth', {}).get('refreshToken')
        }
    except (json.JSONDecodeError, TypeError):
        return None

lines = sys.stdin.read().split('\n', 1)
file_json = lines[0] if len(lines) > 0 else ''
keychain_json = lines[1].rstrip('\n') if len(lines) > 1 else ''

file_tokens = extract_tokens(file_json)
keychain_tokens = extract_tokens(keychain_json)

# If either fails to parse, fall back to raw JSON comparison (fail-safe)
if file_tokens is None or keychain_tokens is None:
    if file_json == keychain_json:
        print('MATCH')
    else:
        print('DIVERGED')
else:
    # Compare token values
    if (file_tokens['accessToken'] == keychain_tokens['accessToken'] and
        file_tokens['refreshToken'] == keychain_tokens['refreshToken']):
        print('MATCH')
    else:
        print('DIVERGED')
")

    if [[ "$result" == "MATCH" ]]; then
        echo "MATCH -- both stores hold the same tokens"
        exit 0
    elif [[ "$result" == "DIVERGED" ]]; then
        # DGN-1050: divergence is the NORMAL steady state -- the CLI writes
        # runtime token rotations to the keychain only and leaves the file
        # stale by design. Informational, exit 0, no action required.
        echo "DIVERGED -- stores differ. NORMAL after a runtime token rotation" \
             "(file stale by design). No action required; do NOT copy the file" \
             "into the keychain. Re-login/account switch: claude auth login"
        exit 0
    else
        die_error "comparison failed: $result"
    fi
}

cmd_sync() {
    # DGN-1050: RETIRED. The unconditional file -> keychain overwrite this
    # subcommand performed re-injected superseded refresh tokens after CLI
    # runtime rotations, triggering server-side token-family revocation and
    # estate-wide auth death. There is no safe direction for a script-driven
    # sync: the file is stale by design and only the CLI may write the store.
    cat >&2 <<'EOF'
RETIRED (DGN-1050): `token-sync.sh sync` no longer exists.

The file -> keychain overwrite it performed is what was killing the
estate's authentication: after every CLI runtime token rotation the
credentials FILE is stale by design, so syncing it into the keychain
re-injected a superseded refresh token; the server revokes the whole
token family on reuse.

The only safe credential writer is the Claude CLI itself:

    claude auth login        (plain terminal, no env vars)

then restart the bridge(s) via their normal restart path.
This script performs no keychain writes of any kind.
EOF
    exit 2
}

# -- dispatch --

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 status   (sync is RETIRED per DGN-1050 -- use: claude auth login)" >&2
    exit 2
fi

case "$1" in
    status) cmd_status ;;
    sync)   cmd_sync   ;;
    *)
        echo "Unknown subcommand: $1" >&2
        echo "Usage: $0 status   (sync is RETIRED per DGN-1050 -- use: claude auth login)" >&2
        exit 2
        ;;
esac
