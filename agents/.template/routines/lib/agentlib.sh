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

# machine_signal TOKEN [part|key=value ...]  -> print ONE machine signal line
# machine_say    TOKEN <sentence...>          -> owner sentence with machine prefix
# DGN-1209: the ONLY sanctioned way to print a machine signal from shell. The
# line carries the in-band sigil (signal::) so the bridge's outbound gate
# drops it (machine_signal) or strips just the prefix (machine_say) on every
# owner-facing rail. Emitting through this helper IS the registration.
# Delegates to routines/lib/machine_signal.py (the wire format lives there).
machine_signal() {
  python3 "$AGENT_ROOT/routines/lib/machine_signal.py" "$@"
}
machine_say() {
  python3 "$AGENT_ROOT/routines/lib/machine_signal.py" --say "$@"
}

# _i18n_files <lang> -> the files one language resolves through, highest
# precedence first, one per line (DGN-1789 layered lookup; python twin:
# routines/lib/i18n_lookup.py layer_paths -- same files, same order):
#   1. kit layers  config/packs/<pack-id>.i18n.<lang>.json -- each installed
#      kit/pack's OWN override layer, written by pack_install.sh. Between two
#      layers overriding one key the first wins, in DEPENDENCY order
#      (DGN-1789 r3): a pack that extends a kit (requires_kit) sits above
#      that kit; name byte order only breaks ties among unrelated layers.
#      The dependency comes from config/packs/<pack-id>.i18n_order
#      ('provides <kit>' / 'above <kit>' lines, written by the same writer);
#      missing record = no known dependency; a cycle = name order for the
#      rest. Algorithm and record grammar: see i18n_lookup.py module doc.
#   2. framework   config/i18n/<lang>.json -- framework defaults, rewritten by
#      every update; kits never write here.
# No instance-level layer exists today; one would go above the kit layers
# (instance > kit > framework default).
# bash 3.2 safe (no associative arrays); set -u safe.
_i18n_files() {
  local LC_ALL=C IFS=$' \t\n' lang="$1" dir="$AGENT_ROOT/config/packs" f id k v x w
  local n=0 i j left picked blocked
  local ids=() prov=() abv=() emitted=()
  for f in "$dir"/*.i18n."$lang".json; do
    [ -f "$f" ] || continue
    id="${f##*/}"; id="${id%.i18n."$lang".json}"
    ids[n]="$id"; prov[n]=" $id "; abv[n]=" "; emitted[n]=""
    if [ -f "$dir/$id.i18n_order" ]; then
      while IFS=$' \t' read -r k v x || [ -n "$k" ]; do
        [ -n "$v" ] && [ -z "$x" ] || continue
        case "$v" in *[!A-Za-z0-9_.-]*) continue ;; esac
        case "$k" in
          provides) prov[n]="${prov[n]}$v " ;;
          above)    abv[n]="${abv[n]}$v " ;;
        esac
      done < "$dir/$id.i18n_order"
    fi
    n=$((n+1))
  done
  left=$n
  while [ "$left" -gt 0 ]; do
    picked=-1
    i=0
    while [ "$i" -lt "$n" ]; do
      if [ -z "${emitted[i]}" ]; then
        blocked=0
        j=0
        while [ "$j" -lt "$n" ] && [ "$blocked" -eq 0 ]; do
          if [ "$j" -ne "$i" ] && [ -z "${emitted[j]}" ]; then
            for w in ${abv[j]}; do
              case "${prov[i]}" in *" $w "*) blocked=1; break ;; esac
            done
          fi
          j=$((j+1))
        done
        if [ "$blocked" -eq 0 ]; then picked=$i; break; fi
      fi
      i=$((i+1))
    done
    if [ "$picked" -lt 0 ]; then
      # cycle: the rest in name order (deterministic; the writer warns)
      i=0
      while [ "$i" -lt "$n" ]; do
        [ -n "${emitted[i]}" ] || printf '%s\n' "$dir/${ids[i]}.i18n.$lang.json"
        i=$((i+1))
      done
      break
    fi
    printf '%s\n' "$dir/${ids[picked]}.i18n.$lang.json"
    emitted[picked]=1
    left=$((left-1))
  done
  printf '%s\n' "$AGENT_ROOT/config/i18n/$1.json"
}

# i18n <key>  -> localized string (current lang, then en, then the key itself).
# Per language: kit override if present, else the framework default.
i18n() {
  local key="$1" lang f val=""
  for lang in "$AGENT_LANG" en; do
    while IFS= read -r f; do
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
    done < <(_i18n_files "$lang")
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

# address -> how the assistant addresses the user (AGENT_ADDRESS override,
# else locale). DGN-1196: keep this phrasing descriptive, not a noun token.
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
