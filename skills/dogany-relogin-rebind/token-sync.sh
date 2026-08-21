#!/usr/bin/env bash
# token-sync.sh -- compare and sync Claude credential token between
# ~/.claude/.credentials.json and macOS Keychain "Claude Code-credentials".
# dogany-relogin-rebind framework skill (DGN-393/394, promoted DGN-591).
#
# PLATFORM: macOS only. Claude CLI reads the login keychain first on Darwin;
# this skill has no counterpart on Linux (no keychain entry to drift), so it
# exits as not-applicable there without touching anything.
#
# Subcommands:
#   status   compare hashes, print result, exit 0=MATCH 1=MISMATCH 2=ERROR
#   sync     overwrite keychain from file, verify MATCH, exit 0=ok 1=fail 2=ERROR

set -euo pipefail

# -- platform guard (must precede any keychain/security call) --
# On non-Darwin hosts there is no login keychain to sync; report clearly and
# exit 3 (not-applicable) WITHOUT running any keychain command. Non-destructive.
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "NOT-APPLICABLE: dogany-relogin-rebind is macOS-only (Keychain sync);" \
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

# Get the account string stored in the existing keychain entry
get_keychain_account() {
    security find-generic-password -s "$KEYCHAIN_SERVICE" 2>/dev/null \
        | grep '"acct"' \
        | sed 's/.*"acct"<blob>="\(.*\)"/\1/' \
        | head -1
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
        print('MISMATCH')
else:
    # Compare token values
    if (file_tokens['accessToken'] == keychain_tokens['accessToken'] and
        file_tokens['refreshToken'] == keychain_tokens['refreshToken']):
        print('MATCH')
    else:
        print('MISMATCH')
")

    echo "$result"

    if [[ "$result" == "MATCH" ]]; then
        exit 0
    elif [[ "$result" == "MISMATCH" ]]; then
        exit 1
    else
        die_error "comparison failed: $result"
    fi
}

cmd_sync() {
    local file_json
    file_json=$(read_file_json)

    # Detect account name used in existing keychain entry
    local acct
    acct=$(get_keychain_account)
    if [[ -z "$acct" ]]; then
        # Fall back to current user if entry absent (first write)
        acct="$USER"
    fi

    # Overwrite keychain entry with full credentials-file JSON
    if ! security add-generic-password -U \
            -s "$KEYCHAIN_SERVICE" \
            -a "$acct" \
            -w "$file_json" 2>/dev/null; then
        die_error "security add-generic-password failed (check Keychain permissions)"
    fi

    echo "Keychain entry updated (account: $acct)"

    # Verify: re-read both and compare using value-level comparison
    local file_tok keychain_tok file_hash keychain_hash
    file_tok=$(read_file_token)
    keychain_tok=$(read_keychain_token)
    file_hash=$(short_hash "$file_tok")
    keychain_hash=$(short_hash "$keychain_tok")

    echo "FILE    token hash: ${file_hash}"
    echo "KEYCHAIN token hash: ${keychain_hash}"

    # Re-read full JSONs for value-level comparison
    local file_json_verify keychain_json_verify
    file_json_verify=$(read_file_json)
    keychain_json_verify=$(security find-generic-password -s "$KEYCHAIN_SERVICE" -w 2>/dev/null) \
        || die_error "keychain entry not found after write: $KEYCHAIN_SERVICE"

    local result
    result=$({
        echo "$file_json_verify"
        echo "$keychain_json_verify"
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
        print('MISMATCH')
else:
    # Compare token values
    if (file_tokens['accessToken'] == keychain_tokens['accessToken'] and
        file_tokens['refreshToken'] == keychain_tokens['refreshToken']):
        print('MATCH')
    else:
        print('MISMATCH')
")

    if [[ "$result" == "MATCH" ]]; then
        echo "MATCH -- sync verified"
        exit 0
    else
        echo "MISMATCH after write -- sync failed" >&2
        exit 1
    fi
}

# -- dispatch --

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 status|sync" >&2
    exit 2
fi

case "$1" in
    status) cmd_status ;;
    sync)   cmd_sync   ;;
    *)
        echo "Unknown subcommand: $1" >&2
        echo "Usage: $0 status|sync" >&2
        exit 2
        ;;
esac
