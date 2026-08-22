#!/bin/bash
# agentlib.sh — shared portability + i18n helpers for skills/routines.
# Source this:  . "$(dirname "${BASH_SOURCE[0]}")/lib/agentlib.sh"
#
# Design goals (for public distribution):
#   - No hardcoded user names or absolute paths. Everything derives from $HOME
#     and the script's own location.
#   - User-specific values (tokens, chat id) come from runtime/.env.
#   - Locale-specific strings (address term, tone guide, headers) come from
#     config/i18n/<lang>.json — i18n style, swappable per user/language.

# AGENT_ROOT = repo root (two levels up from this lib: routines/lib -> root).
if [ -z "${AGENT_ROOT:-}" ]; then
  AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
export AGENT_ROOT

# Load non-secret config (sets AGENT_LANG, AGENT_ADDRESS, ...).
AGENT_CONF="$AGENT_ROOT/config/agent.conf"
[ -f "$AGENT_CONF" ] && . "$AGENT_CONF"
AGENT_LANG="${AGENT_LANG:-en}"

# i18n <key>  -> localized string (current lang, then en, then the key itself).
i18n() {
  local key="$1" dir="$AGENT_ROOT/config/i18n" lang f val=""
  for lang in "$AGENT_LANG" en; do
    f="$dir/$lang.json"
    [ -f "$f" ] || continue
    if command -v jq >/dev/null 2>&1; then
      val=$(jq -r --arg k "$key" '.[$k] // empty' "$f" 2>/dev/null)
    else
      val=$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2],""))
except Exception:
    pass' "$f" "$key" 2>/dev/null)
    fi
    if [ -n "$val" ]; then printf '%s' "$val"; return 0; fi
  done
  printf '%s' "$key"
}

# i18n_or <key> <default> -> localized string; when the key resolves to
# nothing in every locale file (i18n echoes the key itself), fall back to
# <default>. Config-first zero-delta seam (DGN-851): framework scripts pass
# their previous hardcoded copy as <default>, so an instance whose locale
# files lack the key behaves byte-identically to before the seam.
i18n_or() {
  local key="$1" def="$2" val
  val="$(i18n "$key")"
  if [ "$val" = "$key" ]; then printf '%s' "$def"; else printf '%s' "$val"; fi
}

# address -> configured form-of-address (AGENT_ADDRESS override, else locale).
address() {
  if [ -n "${AGENT_ADDRESS:-}" ]; then printf '%s' "$AGENT_ADDRESS"; else i18n address; fi
}

# A portable PATH for generated launchd plists (no hardcoded user dirs).
# $HOME/.local/bin first (DGN-963): the Claude CLI native installer lands
# there and self-updates, so it must win over Homebrew / /usr/local/bin.
# No npm-global entry -- that install method is deprecated (root-owned,
# blocks auto-update).
agent_plist_path() {
  printf '%s' "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
}
